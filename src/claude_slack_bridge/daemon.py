from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import time
import uuid
from pathlib import Path

from aiohttp import web
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from claude_slack_bridge.approval import ApprovalManager
from claude_slack_bridge.config import BridgeConfig
from claude_slack_bridge.process_pool import ProcessPool
from claude_slack_bridge.session_manager import Session, SessionManager, SessionMode
from claude_slack_bridge.slack_client import SlackClient
from claude_slack_bridge.slack_formatter import (
    OPTIONS_ACTION_PREFIX,
    build_approval_blocks,
    build_options_blocks,
    build_permission_denied_blocks,
    build_response_blocks,
    build_session_header_blocks,
    build_tool_notification_blocks,
    build_user_prompt_blocks,
    extract_options,
)
from claude_slack_bridge.stream_parser import StreamEvent

logger = logging.getLogger("claude_slack_bridge")

_SLACK_MAX_TEXT = 3800


def _setup_logging(config: BridgeConfig) -> None:
    log_dir = config.config_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "daemon.log", maxBytes=10 * 1024 * 1024, backupCount=5
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("claude_slack_bridge")
    root.setLevel(getattr(logging, config.log_level, logging.INFO))
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())


class Daemon:
    def __init__(self, config: BridgeConfig) -> None:
        self._config = config
        self._session_mgr = SessionManager(config.config_dir / "sessions.json")
        self._approval_mgr = ApprovalManager()
        self._pool = ProcessPool()
        self._slack: SlackClient | None = None
        self._socket_client: SocketModeClient | None = None
        self._runner: web.AppRunner | None = None
        self._bot_user_id: str = ""
        # Progress message: session_id -> {msg_ts, last_update, lines: list[str]}
        # All tool calls and streaming text go into ONE message, overwritten by final result
        self._progress: dict[str, dict] = {}
        # Trust/YOLO state
        self._trusted_sessions: set[str] = set()
        self._yolo_mode: bool = False
        self._cleanup_task: asyncio.Task | None = None
        # Queued messages: session_id -> [text, ...]
        self._queued: dict[str, list[str]] = {}

    # ── Progress message (single message, overwritten by final result) ──

    async def _update_progress(self, session: Session, line: str) -> None:
        """Update progress message, overwriting previous tool_use lines."""
        sid = session.session_id
        now = time.time()
        if sid not in self._progress:
            msg_ts = await self._slack.post_text(
                session.channel_id, f"🤔 _thinking..._\n{line}", session.thread_ts
            )
            self._progress[sid] = {"msg_ts": msg_ts, "last_update": now, "lines": [line]}
        else:
            state = self._progress[sid]
            state["lines"] = [line]
            # Keep last 8 lines for display
            display = state["lines"][-8:]
            if now - state["last_update"] >= 0.8:
                text = "⏳ " + "\n".join(display)
                try:
                    await self._slack._web.chat_update(
                        channel=session.channel_id, ts=state["msg_ts"], text=text[:_SLACK_MAX_TEXT]
                    )
                    state["last_update"] = now
                except Exception:
                    pass

    async def _finalize_progress(self, session: Session, final_text: str) -> None:
        """Replace progress message with final result using plain text (no 'See more')."""
        sid = session.session_id
        from claude_slack_bridge.slack_formatter import md_to_mrkdwn
        cleaned, choices = extract_options(final_text)
        display = md_to_mrkdwn(cleaned)

        if sid in self._progress:
            state = self._progress.pop(sid)
            msg_ts = state.get("msg_ts")
            if msg_ts:
                try:
                    # Use text field, not blocks — avoids "See more" truncation
                    chunks = _split_text(display, _SLACK_MAX_TEXT)
                    await self._slack._web.chat_update(
                        channel=session.channel_id, ts=msg_ts, text=chunks[0]
                    )
                    # Post remaining chunks as new messages
                    for chunk in chunks[1:]:
                        await self._slack.post_text(
                            session.channel_id, chunk, session.thread_ts
                        )
                except Exception:
                    await self._slack.post_text(
                        session.channel_id, display[:_SLACK_MAX_TEXT], session.thread_ts
                    )
            else:
                await self._slack.post_text(
                    session.channel_id, display[:_SLACK_MAX_TEXT], session.thread_ts
                )
        else:
            titled = f"*Claude Response*\n\n{display}"
            chunks = _split_text(titled, _SLACK_MAX_TEXT)
            for chunk in chunks:
                await self._slack.post_text(
                    session.channel_id, chunk, session.thread_ts
                )

        if choices:
            blocks = build_options_blocks(choices)
            await self._slack.post_blocks(
                session.channel_id, blocks, "Choose an option", session.thread_ts
            )

    # ── Stream event handler (PROCESS mode) ──

    async def _on_stream_event(self, session_id: str, evt: StreamEvent) -> None:
        session = self._session_mgr.get(session_id)
        if not session or not self._slack:
            return
        session.last_active = time.time()  # keep-alive, no disk write

        if evt.raw_type == "assistant" and evt.text:
            sid = session.session_id
            if sid not in self._progress:
                # First token — post "thinking..." placeholder
                msg_ts = await self._slack.post_text(
                    session.channel_id, "🤔 _thinking..._", session.thread_ts
                )
                self._progress[sid] = {"msg_ts": msg_ts, "last_update": 0, "lines": []}
            state = self._progress[sid]
            state["_full_text"] = evt.text

            # Update with streaming preview
            now = time.time()
            if now - state["last_update"] >= 1.0:
                preview = evt.text[-300:] if len(evt.text) > 300 else evt.text
                try:
                    await self._slack._web.chat_update(
                        channel=session.channel_id, ts=state["msg_ts"],
                        text=f"💬 {preview}"[:_SLACK_MAX_TEXT]
                    )
                    state["last_update"] = now
                except Exception:
                    pass

        elif evt.raw_type == "assistant" and evt.tool_use:
            # Clear old tool lines when switching back from text preview
            sid = session.session_id
            if sid in self._progress and self._progress[sid].get("_full_text"):
                self._progress[sid]["lines"] = []
            tool = evt.tool_use
            tool_name = tool.get("name", "")
            tool_input = tool.get("input", {})
            # Show tool call in progress message
            if tool_name == "Bash":
                detail = tool_input.get("command", "")[:60]
            elif tool_name in ("Read", "Write", "Edit"):
                detail = tool_input.get("file_path", "")[:60]
            else:
                detail = tool_name
            await self._update_progress(session, f"🔧 {tool_name}: {detail}")

        elif evt.raw_type == "result":
            sid = session.session_id
            # Get final text from accumulated stream
            final_text = ""
            if sid in self._progress:
                final_text = self._progress[sid].get("_full_text", "")
            if final_text:
                await self._finalize_progress(session, final_text)
            elif sid in self._progress:
                self._progress.pop(sid, None)

            denials = evt.result.get("permission_denials", [])
            if denials:
                blocks = build_permission_denied_blocks(denials)
                await self._slack.post_blocks(
                    session.channel_id, blocks, "Permission denied", session.thread_ts
                )

    async def _on_process_exit(self, session_id: str, rc: int | None) -> None:
        logger.info("Process exited for session %s (rc=%s)", session_id, rc)
        session = self._session_mgr.get(session_id)
        if not session:
            return
        if session.mode == SessionMode.PROCESS.value:
            self._session_mgr.set_mode(session_id, SessionMode.IDLE)
            self._progress.pop(session_id, None)
            if self._slack and rc and rc not in (0, 143):
                await self._slack.post_text(
                    session.channel_id,
                    f"⚠️ Claude process exited unexpectedly (code {rc})",
                    session.thread_ts,
                )

    # ── Session cleanup ──

    async def _cleanup_loop(self) -> None:
        """Periodically clean up stale sessions."""
        while True:
            await asyncio.sleep(60)
            try:
                now = time.time()
                archive_timeout = self._config.session_archive_after_secs
                process_timeout = self._config.process_idle_timeout_secs
                for session in self._session_mgr.list_active():
                    age = now - session.last_active
                    if session.mode == SessionMode.IDLE.value and age > archive_timeout:
                        self._session_mgr.archive(session.session_id)
                        logger.info("Archived stale idle session %s", session.session_id)
                    elif session.mode == SessionMode.PROCESS.value and age > process_timeout:
                        await self._pool.terminate(session.session_id)
                        self._session_mgr.set_mode(session.session_id, SessionMode.IDLE)
                        logger.info("Terminated idle process session %s (no activity for %ds)", session.session_id, int(age))
            except Exception:
                logger.debug("Cleanup error", exc_info=True)

    # ── Socket Mode handlers ──

    async def _on_socket_event(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

        try:
            if req.type == "interactive":
                payload = req.payload or {}
                for action in payload.get("actions", []):
                    await self._handle_interactive(action, payload)

            elif req.type == "events_api":
                event = (req.payload or {}).get("event", {})
                etype = event.get("type", "")

                if etype == "app_mention":
                    await self._handle_mention(event)
                elif etype == "message" and "subtype" not in event and "bot_id" not in event:
                    if event.get("user") == self._bot_user_id:
                        return
                    channel = event.get("channel", "")
                    thread_ts = event.get("thread_ts")
                    if thread_ts:
                        session = self._session_mgr.find_by_thread(channel, thread_ts)
                        if session:
                            await self._handle_thread_reply(event, thread_ts)
                        elif channel.startswith("D"):
                            # Chat mode DM — has thread_ts but no session yet
                            await self._handle_dm(event, thread_ts)
                    elif channel.startswith("D"):
                        await self._handle_dm(event)
        except Exception:
            logger.error("Error handling socket event", exc_info=True)

    async def _handle_mention(self, event: dict) -> None:
        channel_id = event.get("channel", "")
        text = event.get("text", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")

        await self._slack.add_reaction(channel_id, event.get("ts", ""), "eyes")

        prompt = text
        if self._bot_user_id:
            prompt = text.replace(f"<@{self._bot_user_id}>", "").strip()
        if not prompt:
            prompt = "Hello"

        parts = prompt.split()
        if len(parts) >= 2 and parts[0].lower() == "resume":
            await self._handle_resume_cmd(parts, channel_id, thread_ts)
            return

        # Check max concurrent sessions
        active_count = len([s for s in self._session_mgr.list_active()
                           if s.mode == SessionMode.PROCESS.value])
        if active_count >= self._config.max_concurrent_sessions:
            await self._slack.post_text(
                channel_id,
                f"⚠️ Max concurrent sessions ({self._config.max_concurrent_sessions}) reached. "
                "Wait for a session to finish or use an existing thread.",
                thread_ts,
            )
            return

        await self._start_new_session(channel_id, thread_ts, prompt)

    async def _handle_dm(self, event: dict, thread_ts: str | None = None) -> None:
        channel_id = event.get("channel", "")
        text = event.get("text", "")
        msg_ts = event.get("ts", "")
        # Chat mode provides thread_ts; traditional DM uses msg_ts as thread root
        effective_ts = thread_ts or msg_ts

        await self._slack.add_reaction(channel_id, msg_ts, "eyes")

        if self._bot_user_id:
            text = text.replace(f"<@{self._bot_user_id}>", "").strip()
        if not text:
            text = "Hello"

        parts = text.split()
        if len(parts) >= 2 and parts[0].lower() == "resume":
            await self._handle_resume_cmd(parts, channel_id, effective_ts)
            return

        active_count = len([s for s in self._session_mgr.list_active()
                           if s.mode == SessionMode.PROCESS.value])
        if active_count >= self._config.max_concurrent_sessions:
            await self._slack.post_text(
                channel_id,
                f"⚠️ Max concurrent sessions ({self._config.max_concurrent_sessions}) reached.",
                effective_ts,
            )
            return

        await self._start_new_session(channel_id, effective_ts, text)

    async def _handle_resume_cmd(self, parts: list[str], channel_id: str, thread_ts: str) -> None:
        """Handle 'resume <session-id> [follow-up]' command."""
        sid = parts[1]
        session = self._session_mgr.get(sid)
        if not session:
            session = self._register_external_session(sid, channel_id, thread_ts)
        if session:
            session.channel_id = channel_id
            session.thread_ts = thread_ts
            self._session_mgr._thread_index[(channel_id, thread_ts)] = session.session_id
            self._session_mgr._save()
            cwd = getattr(session, "_cwd", None) or self._config.work_dir
            blocks = build_session_header_blocks(session_id=session.session_id, directory=cwd)
            await self._slack.post_blocks(
                channel_id, blocks, f"Resumed: {session.session_name}", thread_ts
            )
            follow_up = " ".join(parts[2:]) if len(parts) > 2 else None
            await self._resume_process(session, follow_up)
        else:
            await self._slack.post_text(channel_id, f"❌ Session `{sid}` not found", thread_ts)

    def _register_external_session(
        self, sid: str, channel_id: str, thread_ts: str
    ) -> Session | None:
        """Find a Claude Code session file and register it in daemon."""
        import json as _json
        claude_dir = Path.home() / ".claude" / "projects"
        if not claude_dir.is_dir():
            return None
        for jsonl in claude_dir.rglob(f"{sid}*.jsonl"):
            name = sid[:12]
            try:
                first = _json.loads(jsonl.read_text().split("\n", 1)[0])
                name = first.get("customTitle", name) or name
            except Exception:
                pass
            # Derive cwd: project dir is path with / replaced by -
            # e.g. -local-home-qianheng → try progressively longer paths
            cwd = _decode_project_dir(jsonl.parent.name)
            session = self._session_mgr.create(
                session_id=jsonl.stem,
                session_name=name[:30],
                channel_id=channel_id,
                thread_ts=thread_ts,
                mode=SessionMode.IDLE,
            )
            session._cwd = cwd
            return session
        return None

    async def _start_new_session(self, channel_id: str, thread_ts: str, prompt: str) -> None:
        session_id = str(uuid.uuid4())
        name = prompt[:30].replace("\n", " ")

        blocks = build_session_header_blocks(
            session_id=session_id, directory=self._config.work_dir
        )
        await self._slack.post_blocks(channel_id, blocks, f"Session: {name}", thread_ts)

        self._session_mgr.create(
            session_id=session_id,
            session_name=name,
            channel_id=channel_id,
            thread_ts=thread_ts,
            mode=SessionMode.PROCESS,
        )

        await self._pool.start(
            session_id=session_id,
            prompt=prompt,
            name=name,
            cwd=self._config.work_dir,
            extra_args=self._config.claude_args,
            on_event=self._on_stream_event,
            on_exit=self._on_process_exit,
        )

    async def _handle_thread_reply(self, event: dict, thread_ts: str) -> None:
        channel_id = event.get("channel", "")
        text = event.get("text", "")

        session = self._session_mgr.find_by_thread(channel_id, thread_ts)
        if not session:
            return

        await self._slack.add_reaction(channel_id, event.get("ts", ""), "eyes")
        session.touch()

        lower = text.strip().lower()
        if lower == "yolo off":
            self._yolo_mode = False
            self._trusted_sessions.discard(session.session_id)
            await self._slack.post_text(channel_id, "🔒 YOLO/Trust mode disabled", thread_ts)
            return

        if session.mode == SessionMode.PROCESS.value:
            cp = self._pool.get(session.session_id)
            if cp:
                await cp.send_message(text)
            else:
                await self._resume_process(session, text)
        elif self._is_tui_active(session):
            # TUI is running — start --print alongside, won't sync to TUI
            await self._resume_process(session, text)
        elif session.mode == SessionMode.IDLE.value:
            await self._resume_process(session, text)

    async def _resume_process(self, session: Session, text: str | None) -> None:
        self._session_mgr.set_mode(session.session_id, SessionMode.PROCESS)
        cwd = getattr(session, "_cwd", None) or self._config.work_dir
        await self._pool.start(
            session_id=session.session_id,
            prompt=text,
            resume=True,
            name=session.session_name,
            cwd=cwd,
            extra_args=self._config.claude_args,
            on_event=self._on_stream_event,
            on_exit=self._on_process_exit,
        )

    async def _auto_bind_session(self, session_key: str, cwd: str) -> Session | None:
        """Auto-bind a TUI session to a Slack DM thread for the approval flow.

        When a PreToolUse hook arrives from a TUI session that has no Slack
        binding yet, this creates a DM thread so approval buttons can be
        posted there (Issue #2).
        """
        if not self._slack:
            return None
        try:
            resp = await self._slack._web.conversations_list(types="im", limit=1)
            ims = resp.get("channels", [])
            if not ims:
                logger.warning("No DM channel found for auto-bind")
                return None
            dm_channel = ims[0]["id"]

            name = f"TUI-{session_key[:12]}"
            blocks = build_session_header_blocks(
                session_id=session_key, directory=cwd or self._config.work_dir
            )
            thread_ts = await self._slack.post_blocks(
                dm_channel, blocks, f"Session: {name}"
            )

            session = self._session_mgr.create(
                session_id=session_key,
                session_name=name,
                channel_id=dm_channel,
                thread_ts=thread_ts,
                mode=SessionMode.HOOK,
            )
            session._cwd = cwd
            logger.info("Auto-bound TUI session %s to DM %s", session_key, dm_channel)
            return session
        except Exception:
            logger.error("Failed to auto-bind session %s", session_key, exc_info=True)
            return None

    def _is_tui_active(self, session: Session) -> bool:
        """Check if TUI was recently active (hook received within 30s)."""
        tui_ts = getattr(session, "_tui_active", 0)
        return time.time() - tui_ts < 30

    async def _drain_queue(self, session: Session) -> None:
        """Send queued Slack messages to Claude after TUI exits."""
        msgs = self._queued.pop(session.session_id, [])
        if not msgs and not self._slack:
            return
        if msgs:
            await self._slack.post_text(
                session.channel_id,
                f"🚀 TUI 已退出，发送 {len(msgs)} 条排队消息...",
                session.thread_ts,
            )
            # Start --print and send messages
            await self._resume_process(session, msgs[0])
            # Wait for process to be ready, then send remaining
            if len(msgs) > 1:
                await asyncio.sleep(2)
                cp = self._pool.get(session.session_id)
                if cp:
                    for msg in msgs[1:]:
                        # Wait for previous response before sending next
                        await asyncio.sleep(1)
                        await cp.send_message(msg)

    async def _handle_interactive(self, action: dict, payload: dict) -> None:
        action_id = action.get("action_id", "")
        value = action.get("value", "")
        channel_info = payload.get("channel", {})
        channel_id = channel_info.get("id", "")
        msg = payload.get("message", {})
        msg_ts = msg.get("ts", "")

        if action_id == "approve_tool":
            self._approval_mgr.resolve(value, "approved")
        elif action_id == "reject_tool":
            self._approval_mgr.resolve(value, "rejected")
        elif action_id.startswith(OPTIONS_ACTION_PREFIX):
            thread_ts = msg.get("thread_ts", msg_ts)
            if channel_id and thread_ts:
                session = self._session_mgr.find_by_thread(channel_id, thread_ts)
                if session:
                    cp = self._pool.get(session.session_id)
                    if cp:
                        await cp.send_message(value)
                    if self._slack and msg_ts:
                        try:
                            await self._slack._web.chat_delete(channel=channel_id, ts=msg_ts)
                        except Exception:
                            pass
        elif action_id == "trust_session":
            self._trusted_sessions.add(value)
            for state in list(self._approval_mgr._pending.values()):
                state.resolve("approved")
            if self._slack and channel_id and msg_ts:
                from claude_slack_bridge.slack_formatter import build_approval_resolved_blocks
                blocks = build_approval_resolved_blocks("Session", "trusted", value)
                try:
                    await self._slack.update_blocks(channel_id, msg_ts, blocks)
                except Exception:
                    pass
        elif action_id == "yolo_mode":
            self._yolo_mode = True
            self._approval_mgr.resolve(value, "approved")
            if self._slack and channel_id and msg_ts:
                from claude_slack_bridge.slack_formatter import build_approval_resolved_blocks
                blocks = build_approval_resolved_blocks("Global", "YOLO enabled", value)
                try:
                    await self._slack.update_blocks(channel_id, msg_ts, blocks)
                except Exception:
                    pass

        elif action_id == "takeover_session":
            # Kill TUI process and drain queue via --resume --print
            session = self._session_mgr.get(value)
            if session:
                session._tui_active = 0  # Clear TUI active flag
                await self._drain_queue(session)
                if self._slack and msg_ts:
                    try:
                        await self._slack._web.chat_update(
                            channel=channel_id, ts=msg_ts,
                            text="⚡ Slack 已接管 session"
                        )
                    except Exception:
                        pass

    # ── HTTP API ──

    def _create_http_app(self) -> web.Application:
        routes = web.RouteTableDef()

        @routes.get("/health")
        async def health(req: web.Request) -> web.Response:
            return web.json_response({"status": "ok"})

        @routes.get("/sessions")
        async def list_sessions(req: web.Request) -> web.Response:
            active = self._session_mgr.list_active()
            return web.json_response([
                {"session_id": s.session_id, "name": s.session_name,
                 "mode": s.mode, "channel_id": s.channel_id}
                for s in active
            ])

        @routes.post("/sessions/bind")
        async def bind_session(req: web.Request) -> web.Response:
            """Bind a TUI session to a new Slack DM thread."""
            payload = await req.json()
            session_id = payload.get("session_id", "")
            session_name = payload.get("name", session_id[:12])
            cwd = payload.get("cwd", self._config.work_dir)

            if not self._slack or not self._bot_user_id:
                return web.json_response({"error": "slack not connected"}, status=503)

            # Find or open DM with bot owner
            owner_id = payload.get("owner_id", "")
            if not owner_id:
                # Use first DM channel we know about
                resp = await self._slack._web.conversations_list(types="im", limit=1)
                ims = resp.get("channels", [])
                if ims:
                    dm_channel = ims[0]["id"]
                else:
                    return web.json_response({"error": "no DM channel found"}, status=404)
            else:
                resp = await self._slack._web.conversations_open(users=owner_id)
                dm_channel = resp["channel"]["id"]

            # Reuse existing thread if session already bound
            session = self._session_mgr.get(session_id)
            if session and session.thread_ts and session.channel_id == dm_channel:
                thread_ts = session.thread_ts
            else:
                # New session — post header as thread root
                blocks = build_session_header_blocks(session_id=session_id, directory=cwd)
                thread_ts = await self._slack.post_blocks(
                    dm_channel, blocks, f"Session: {session_name}"
                )
                if not session:
                    session = self._session_mgr.create(
                        session_id=session_id,
                        session_name=session_name,
                        channel_id=dm_channel,
                        thread_ts=thread_ts,
                        mode=SessionMode.IDLE,
                    )
                else:
                    session.channel_id = dm_channel
                    session.thread_ts = thread_ts
                    self._session_mgr._thread_index[(dm_channel, thread_ts)] = session.session_id
                    self._session_mgr._save()
            session._cwd = cwd

            return web.json_response({
                "ok": True,
                "channel_id": dm_channel,
                "thread_ts": thread_ts,
                "session_id": session_id,
            })

        @routes.post("/hooks/{hook_type}")
        async def hook_handler(req: web.Request) -> web.Response:
            hook_type = req.match_info["hook_type"]
            payload = await req.json()
            session_key = payload.get("session_key", "")

            session = self._session_mgr.get(session_key)

            # pre-tool-use can arrive from unbound TUI sessions — handle
            # before the session-required checks below (Issue #2).
            if hook_type == "pre-tool-use":
                tool_name = payload.get("tool_name", "")
                tool_input = payload.get("tool_input", {})

                # Fast-path: YOLO mode — approve everything
                if self._yolo_mode:
                    return web.Response(text="approved")

                # Fast-path: trusted session
                if session and session.session_id in self._trusted_sessions:
                    return web.Response(text="approved")

                # Fast-path: safe tools (Read, Glob, Grep by default)
                if tool_name in self._config.auto_approve_tools:
                    return web.Response(text="approved")

                # Need Slack connection to post approval buttons
                if not self._slack:
                    return web.Response(text="approved")

                # Auto-bind session to a Slack DM if not yet registered
                if not session:
                    session = await self._auto_bind_session(
                        session_key, payload.get("cwd", "")
                    )
                    if not session:
                        # Cannot reach Slack — fail open so TUI isn't blocked
                        return web.Response(text="approved")

                # Post approval buttons to the Slack thread
                request_id = str(uuid.uuid4())
                blocks = build_approval_blocks(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    session_id=session.session_id,
                    session_name=session.session_name,
                    request_id=request_id,
                )
                await self._slack.post_blocks(
                    session.channel_id,
                    blocks,
                    f"🔐 Approve {tool_name}?",
                    session.thread_ts,
                )

                # Block until user clicks Approve/Reject or timeout
                state = self._approval_mgr.create(request_id)
                result = await state.wait(
                    timeout=self._config.approval_timeout_secs
                )
                self._approval_mgr.cleanup(request_id)

                return web.Response(
                    text="approved" if result == "approved" else "rejected"
                )

            # Other hooks require an existing session
            if not session:
                return web.json_response({"error": "unknown session"}, status=404)

            # TUI hook arrived — just sync to Slack, don't kill --print
            session.touch()
            session._tui_active = time.time()

            # Sync TUI content to Slack
            if hook_type == "user-prompt" and self._slack and session.channel_id:
                blocks = build_user_prompt_blocks(payload.get("prompt", ""))
                await self._slack.post_blocks(
                    session.channel_id, blocks, "User prompt (TUI)", session.thread_ts
                )
            elif hook_type == "stop" and self._slack and session.channel_id:
                response_text = payload.get("response", "")
                if response_text:
                    await self._finalize_progress(session, response_text)
                # TUI exited — drain queued Slack messages
                await self._drain_queue(session)

            return web.Response(text="ok")

        app = web.Application()
        app.router.add_routes(routes)
        return app

    # ── Lifecycle ──

    async def start(self) -> None:
        _setup_logging(self._config)
        logger.info("Starting Bridge Daemon on port %d", self._config.daemon_port)

        self._slack = SlackClient(self._config.slack_bot_token)

        try:
            auth = await self._slack.auth_test()
            self._bot_user_id = auth.get("user_id", "")
            logger.info("Bot user ID: %s", self._bot_user_id)
        except Exception:
            logger.warning("Failed to get bot user ID", exc_info=True)

        app = self._create_http_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._config.daemon_port)
        try:
            await site.start()
        except OSError as e:
            logger.error("Port %d in use: %s", self._config.daemon_port, e)
            raise SystemExit(1) from e
        logger.info("HTTP API listening on 127.0.0.1:%d", self._config.daemon_port)

        if self._config.slack_app_token and self._config.slack_bot_token:
            self._socket_client = SocketModeClient(
                app_token=self._config.slack_app_token,
                web_client=self._slack.web,
            )
            self._socket_client.socket_mode_request_listeners.append(self._on_socket_event)
            await self._socket_client.connect()
            logger.info("Slack Socket Mode connected")
        else:
            logger.warning("Slack tokens not configured — HTTP-only mode")

        # Start cleanup loop
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        pid_file = self._config.config_dir / "daemon.pid"
        pid_file.write_text(str(os.getpid()))

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()

        logger.info("Shutting down...")
        await self.stop()

    async def stop(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
        await self._pool.terminate_all()
        if self._socket_client:
            await self._socket_client.close()
        if self._runner:
            await self._runner.cleanup()
        pid_file = self._config.config_dir / "daemon.pid"
        pid_file.unlink(missing_ok=True)


def _decode_project_dir(encoded: str) -> str:
    """Decode Claude Code project dir name back to filesystem path.

    Claude encodes /local/home/user as -local-home-user.
    We greedily try to find the longest existing path.
    """
    raw = encoded.lstrip("-")
    parts = raw.split("-")
    # Greedy: try combining segments with - or / to find longest existing path
    best = ""
    _try_paths(parts, 0, "", best_ref := [best])
    return best_ref[0] or ("/" + raw.replace("-", "/"))


def _try_paths(parts: list[str], idx: int, current: str, best: list[str]) -> None:
    """Recursively try combining remaining parts with / or - to find existing dirs."""
    if idx >= len(parts):
        if os.path.isdir(current) and len(current) > len(best[0]):
            best[0] = current
        return
    # Try adding next part with /
    with_slash = f"{current}/{parts[idx]}"
    _try_paths(parts, idx + 1, with_slash, best)
    # Try adding next part with - (merge into current segment)
    if current and not current.endswith("/"):
        with_dash = f"{current}-{parts[idx]}"
        _try_paths(parts, idx + 1, with_dash, best)


def _split_text(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, max_len)
        if cut <= 0:
            cut = max_len
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks
