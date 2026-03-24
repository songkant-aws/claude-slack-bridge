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
    build_approval_blocks,
    build_permission_denied_blocks,
    build_response_blocks,
    build_session_header_blocks,
    build_tool_notification_blocks,
    build_user_prompt_blocks,
)
from claude_slack_bridge.stream_parser import StreamEvent

logger = logging.getLogger("claude_slack_bridge")


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
        # Throttle state: session_id -> (msg_ts, last_update, accumulated_text)
        self._stream_state: dict[str, dict] = {}
        # Trust/YOLO state
        self._trusted_sessions: set[str] = set()  # session_ids with trust mode
        self._yolo_mode: bool = False  # global auto-approve

    # ── Stream event handler (PROCESS mode: claude stdout → Slack) ──

    async def _on_stream_event(self, session_id: str, evt: StreamEvent) -> None:
        session = self._session_mgr.get(session_id)
        if not session or not self._slack:
            return

        if evt.raw_type == "assistant" and evt.text:
            await self._stream_text_to_slack(session, evt.text)

        elif evt.raw_type == "assistant" and evt.tool_use:
            tool = evt.tool_use
            tool_name = tool.get("name", "")
            tool_input = tool.get("input", {})
            # In PROCESS mode, claude auto-executes tools based on permission-mode.
            # We just notify in Slack.
            blocks = build_tool_notification_blocks(tool_name, tool_input)
            await self._slack.post_blocks(
                session.channel_id, blocks, f"Tool: {tool_name}", session.thread_ts
            )
            # Flush any accumulated stream text before tool notification
            if session_id in self._stream_state:
                state = self._stream_state.pop(session_id)
                await self._flush_stream(session, state)

        elif evt.raw_type == "result":
            # Flush remaining streamed text
            if session_id in self._stream_state:
                state = self._stream_state.pop(session_id)
                if state.get("text"):
                    await self._flush_stream(session, state)
            # Report permission denials
            denials = evt.result.get("permission_denials", [])
            if denials:
                blocks = build_permission_denied_blocks(denials)
                await self._slack.post_blocks(
                    session.channel_id, blocks, "Permission denied", session.thread_ts
                )

    async def _stream_text_to_slack(self, session: Session, text: str) -> None:
        """Throttled streaming: update Slack message at most every 1s."""
        sid = session.session_id
        now = time.time()

        if sid not in self._stream_state:
            # First message: post new
            blocks = build_response_blocks(text)
            msg_ts = await self._slack.post_blocks(
                session.channel_id, blocks, "Claude response", session.thread_ts
            )
            self._stream_state[sid] = {
                "msg_ts": msg_ts, "last_update": now, "text": text
            }
        else:
            state = self._stream_state[sid]
            state["text"] = text
            if now - state["last_update"] >= 1.0:
                await self._flush_stream(session, state)

    async def _flush_stream(self, session: Session, state: dict) -> None:
        blocks = build_response_blocks(state["text"])
        try:
            await self._slack.update_blocks(
                session.channel_id, state["msg_ts"], blocks
            )
            state["last_update"] = time.time()
        except Exception:
            logger.debug("Failed to update stream message", exc_info=True)

    async def _on_process_exit(self, session_id: str, rc: int | None) -> None:
        logger.info("Process exited for session %s (rc=%s)", session_id, rc)
        session = self._session_mgr.get(session_id)
        if session and session.mode == SessionMode.PROCESS.value:
            self._session_mgr.set_mode(session_id, SessionMode.IDLE)
            self._stream_state.pop(session_id, None)

    # ── Socket Mode handlers ──

    async def _on_socket_event(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

        if req.type == "interactive":
            payload = req.payload or {}
            for action in payload.get("actions", []):
                await self._handle_interactive(action, payload)

        elif req.type == "events_api":
            event = (req.payload or {}).get("event", {})
            etype = event.get("type", "")

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
                # Skip messages from our own bot user
                if event.get("user") == self._bot_user_id:
                    return
                thread_ts = event.get("thread_ts")
                if thread_ts:
                    await self._handle_thread_reply(event, thread_ts)

    async def _handle_mention(self, event: dict) -> None:
        """Handle @bridge mention — create new session in PROCESS mode."""
        channel_id = event.get("channel", "")
        text = event.get("text", "")
        # Use existing thread_ts if in a thread, otherwise use message ts as thread root
        thread_ts = event.get("thread_ts") or event.get("ts", "")

        # Strip bot mention from text
        prompt = text
        if self._bot_user_id:
            prompt = text.replace(f"<@{self._bot_user_id}>", "").strip()
        if not prompt:
            prompt = "Hello"

        session_id = str(uuid.uuid4())
        name = prompt[:30].replace("\n", " ")

        # Post session header
        blocks = build_session_header_blocks(session_id=session_id, directory="")
        header_ts = await self._slack.post_blocks(
            channel_id, blocks, f"Session: {name}", thread_ts
        )

        # Create session
        session = self._session_mgr.create(
            session_id=session_id,
            session_name=name,
            channel_id=channel_id,
            thread_ts=thread_ts,
            mode=SessionMode.PROCESS,
        )

        # Start claude --print
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
        """Route thread reply based on session mode."""
        channel_id = event.get("channel", "")
        text = event.get("text", "")
        user = event.get("user", "")

        session = self._session_mgr.find_by_thread(channel_id, thread_ts)
        if not session:
            return

        session.touch()

        if session.mode == SessionMode.PROCESS.value:
            cp = self._pool.get(session.session_id)
            if cp:
                await cp.send_message(text)
            else:
                # Process died, restart
                await self._resume_process(session, text)

        elif session.mode == SessionMode.HOOK.value:
            await self._slack.post_text(
                channel_id,
                "⚠️ Session 正在 TUI 中使用，请在终端操作，或退出 TUI 后从 Slack 继续",
                thread_ts,
            )

        elif session.mode == SessionMode.IDLE.value:
            await self._resume_process(session, text)

    async def _resume_process(self, session: Session, text: str) -> None:
        """Resume a session in PROCESS mode."""
        self._session_mgr.set_mode(session.session_id, SessionMode.PROCESS)
        await self._pool.start(
            session_id=session.session_id,
            prompt=text,
            resume=True,
            name=session.session_name,
            cwd=self._config.work_dir,
            extra_args=self._config.claude_args,
            on_event=self._on_stream_event,
            on_exit=self._on_process_exit,
        )

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

        elif action_id == "trust_session":
            # value = session_id
            self._trusted_sessions.add(value)
            # Resolve any pending approval for this session
            for state in list(self._approval_mgr._pending.values()):
                state.resolve("approved")
            if self._slack and channel_id and msg_ts:
                from claude_slack_bridge.slack_formatter import build_approval_resolved_blocks
                blocks = build_approval_resolved_blocks("Session", "trusted", value)
                try:
                    await self._slack.update_blocks(channel_id, msg_ts, blocks)
                except Exception:
                    pass
            logger.info("Trust mode enabled for session %s", value)

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
            logger.info("YOLO mode enabled globally")

    # ── HTTP API (for HOOK mode / TUI) ──

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

        @routes.post("/hooks/{hook_type}")
        async def hook_handler(req: web.Request) -> web.Response:
            """Handle TUI hooks — switch session to HOOK mode."""
            hook_type = req.match_info["hook_type"]
            payload = await req.json()
            session_key = payload.get("session_key", "")

            session = self._session_mgr.get(session_key)
            if not session:
                return web.json_response({"error": "unknown session"}, status=404)

            # If session is in PROCESS mode AND our --print process is alive,
            # hooks come from our --print process — just ack without switching.
            # (--print processes should use --setting-sources to skip hooks,
            #  but this is a safety net.)
            if session.mode == SessionMode.PROCESS.value and self._pool.get(session.session_id):
                session.touch()
                if hook_type == "pre-tool-use":
                    return web.Response(text="approved")
                return web.Response(text="ok")

            # Switch to HOOK mode (TUI is active), kill --print process if running
            if session.mode != SessionMode.HOOK.value:
                await self._pool.terminate(session.session_id)
                self._session_mgr.set_mode(session.session_id, SessionMode.HOOK)
                logger.info("Session %s switched to HOOK mode", session.session_id)

            session.touch()

            # Forward hook content to Slack
            if hook_type == "user-prompt" and self._slack:
                blocks = build_user_prompt_blocks(payload.get("prompt", ""))
                await self._slack.post_blocks(
                    session.channel_id, blocks, "User prompt", session.thread_ts
                )
            elif hook_type == "stop" and self._slack:
                response_text = payload.get("response", "")
                if response_text:
                    blocks = build_response_blocks(response_text)
                    await self._slack.post_blocks(
                        session.channel_id, blocks, "Claude response", session.thread_ts
                    )
                # TUI turn ended — switch back to IDLE so Slack can resume
                self._session_mgr.set_mode(session.session_id, SessionMode.IDLE)
                logger.info("Session %s switched to IDLE (TUI stop)", session.session_id)
            elif hook_type == "pre-tool-use" and self._slack:
                tool_name = payload.get("tool_name", "")
                tool_input = payload.get("tool_input", {})

                # Auto-approve: whitelist, trust, or yolo
                if (not self._config.require_approval
                        or tool_name in self._config.auto_approve_tools
                        or self._yolo_mode
                        or session_key in self._trusted_sessions):
                    # Notify in Slack
                    blocks = build_tool_notification_blocks(tool_name, tool_input)
                    await self._slack.post_blocks(
                        session.channel_id, blocks, f"Auto-approved: {tool_name}", session.thread_ts
                    )
                    return web.Response(text="approved")

                # Interactive approval: post buttons, wait for click
                request_id = payload.get("request_id", str(uuid.uuid4()))
                state = self._approval_mgr.create(request_id)

                blocks = build_approval_blocks(
                    tool_name, tool_input, session_key,
                    session.session_name, request_id,
                )
                await self._slack.post_blocks(
                    session.channel_id, blocks, f"Approve {tool_name}?", session.thread_ts
                )

                # Block until user clicks button or timeout
                decision = await state.wait(timeout=self._config.approval_timeout_secs)
                self._approval_mgr.cleanup(request_id)

                # If trust was enabled during wait, approve
                if session_key in self._trusted_sessions or self._yolo_mode:
                    decision = "approved"

                return web.Response(text=decision if decision != "timed_out" else "rejected")

            return web.Response(text="ok")

        app = web.Application()
        app.router.add_routes(routes)
        return app

    # ── Lifecycle ──

    async def start(self) -> None:
        _setup_logging(self._config)
        logger.info("Starting Bridge Daemon on port %d", self._config.daemon_port)

        self._slack = SlackClient(self._config.slack_bot_token)

        # Get bot user ID for mention stripping
        try:
            auth = await self._slack.auth_test()
            self._bot_user_id = auth.get("user_id", "")
            logger.info("Bot user ID: %s", self._bot_user_id)
        except Exception:
            logger.warning("Failed to get bot user ID", exc_info=True)

        # HTTP API
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

        # Socket Mode
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

        # PID file
        pid_file = self._config.config_dir / "daemon.pid"
        pid_file.write_text(str(os.getpid()))

        # Wait for shutdown signal
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()

        logger.info("Shutting down...")
        await self.stop()

    async def stop(self) -> None:
        await self._pool.terminate_all()
        if self._socket_client:
            await self._socket_client.close()
        if self._runner:
            await self._runner.cleanup()
        pid_file = self._config.config_dir / "daemon.pid"
        pid_file.unlink(missing_ok=True)
