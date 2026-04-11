from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import uuid
from pathlib import Path

from aiohttp import web
from slack_sdk.socket_mode.aiohttp import SocketModeClient

from claude_slack_bridge.approval import ApprovalManager
from claude_slack_bridge.config import BridgeConfig
from claude_slack_bridge.conversation_parser import ConversationParser, SessionFileWatcher
from claude_slack_bridge.daemon_events import EventsMixin
from claude_slack_bridge.daemon_http import create_http_app
from claude_slack_bridge.daemon_stream import StreamMixin
from claude_slack_bridge.daemon_utils import SeenCache, decode_project_dir, setup_logging
from claude_slack_bridge.process_pool import ProcessPool
from claude_slack_bridge.session_manager import Session, SessionManager, SessionMode
from claude_slack_bridge.slack_client import SlackClient
from claude_slack_bridge.reactions import StatusReactionController
from claude_slack_bridge.slack_formatter import (
    build_session_header_blocks,
)

logger = logging.getLogger("claude_slack_bridge")


class Daemon(StreamMixin, EventsMixin):
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
        # Event dedup cache — prevents duplicate Slack event processing
        self._seen = SeenCache()
        # Queued messages: session_id -> [text, ...]
        self._queued: dict[str, list[str]] = {}
        # Sessions with TUI→Slack sync muted
        self._tui_sync_muted: set[str] = set()
        # Prompts forwarded from Slack→tmux — skip echo back via UserPromptSubmit hook
        self._forwarded_prompts: set[str] = set()
        # Pending approval messages: session_id -> msg_ts (for cleanup on TUI approval)
        self._pending_approval_msgs: dict[str, str] = {}
        # Sessions that have been finalized this turn — JSONL watcher skips these
        self._finalized_sessions: set[str] = set()
        # Active reaction controllers per session (for TUI hook updates)
        self._reaction_controllers: dict[str, StatusReactionController] = {}
        # Conversation parser (Channel 2: JSONL file monitoring)
        self._conv_parser = ConversationParser()
        self._file_watcher = SessionFileWatcher(
            self._conv_parser, on_new_messages=self._on_jsonl_messages
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

    # ── Session management ──

    async def _start_new_session(
        self, channel_id: str, thread_ts: str, prompt: str,
        reaction_controller: StatusReactionController | None = None,
    ) -> None:
        session_id = str(uuid.uuid4())
        name = prompt[:30].replace("\n", " ")

        blocks = build_session_header_blocks(
            session_id=session_id, directory=self._config.work_dir
        )
        await self._slack.post_blocks(channel_id, blocks, f"Session: {name}", thread_ts)

        session = self._session_mgr.create(
            session_id=session_id,
            session_name=name,
            channel_id=channel_id,
            thread_ts=thread_ts,
            mode=SessionMode.PROCESS,
        )
        session.origin = "slack"

        # Pre-seed progress state with reaction controller
        if reaction_controller:
            self._progress[session_id] = {
                "msg_ts": None,
                "last_update": 0,
                "lines": [],
                "_full_text": "",
                "_bracket_hold": "",
                "_reactions": reaction_controller,
            }

        await self._pool.start(
            session_id=session_id,
            prompt=prompt,
            name=name,
            cwd=self._config.work_dir,
            extra_args=self._config.claude_args,
            on_event=self._on_stream_event,
            on_exit=self._on_process_exit,
        )

    async def _resume_process(self, session: Session, text: str | None) -> None:
        self._session_mgr.set_mode(session.session_id, SessionMode.PROCESS)
        cwd = session.cwd or self._config.work_dir
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
            cwd = session.cwd or self._config.work_dir
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
            cwd = decode_project_dir(jsonl.parent.name)
            session = self._session_mgr.create(
                session_id=jsonl.stem,
                session_name=name[:30],
                channel_id=channel_id,
                thread_ts=thread_ts,
                mode=SessionMode.IDLE,
            )
            session.cwd = cwd
            return session
        return None

    async def _auto_bind_session(self, session_key: str, cwd: str) -> Session | None:
        """Auto-bind a TUI session to a Slack DM thread for the approval flow.

        When a PreToolUse hook arrives from a TUI session that has no Slack
        binding yet, this creates a DM thread so approval buttons can be
        posted there (Issue #2).
        """
        if not self._slack:
            return None
        try:
            resp = await self._slack.web.conversations_list(types="im", limit=1)
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
            session.cwd = cwd
            session.origin = "tui"
            logger.info("Auto-bound TUI session %s to DM %s", session_key, dm_channel)
            return session
        except Exception:
            logger.error("Failed to auto-bind session %s", session_key, exc_info=True)
            return None

    def _is_tui_active(self, session: Session) -> bool:
        """Check if TUI was recently active (hook received within 30s)."""
        tui_ts = session.tui_active
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
            # Start --print with first message, then resume for remaining
            await self._resume_process(session, msgs[0])
            for msg in msgs[1:]:
                # Wait for previous to finish before sending next
                cp = self._pool.get(session.session_id)
                if cp:
                    try:
                        await asyncio.wait_for(cp._init_event.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        logger.warning("Drain queue: init timeout for %s", session.session_id)
                        return
                    # Wait for process to finish before resuming with next msg
                    if cp._reader_task:
                        await cp._reader_task
                await self._resume_process(session, msg)

    # ── Lifecycle ──

    async def start(self) -> None:
        setup_logging(self._config)
        logger.info("Starting Bridge Daemon on port %d", self._config.daemon_port)

        # Log unhandled exceptions in fire-and-forget tasks
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(self._loop_exception_handler)

        try:
            await self._start_services()

            pid_file = self._config.config_dir / "daemon.pid"
            pid_file.write_text(str(os.getpid()))

            stop_event = asyncio.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop_event.set)
            await stop_event.wait()

            logger.info("Shutting down...")
        except Exception:
            logger.critical("Daemon crashed", exc_info=True)
        finally:
            await self.stop()

    async def _start_services(self) -> None:
        """Start HTTP, Slack Socket Mode, and cleanup loop."""
        self._slack = SlackClient(self._config.slack_bot_token)

        try:
            auth = await self._slack.auth_test()
            self._bot_user_id = auth.get("user_id", "")
            logger.info("Bot user ID: %s", self._bot_user_id)
        except Exception:
            logger.warning("Failed to get bot user ID", exc_info=True)

        app = create_http_app(self)
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

    @staticmethod
    def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        msg = context.get("message", "Unhandled async exception")
        if exc:
            logger.error("%s: %s", msg, exc, exc_info=exc)
        else:
            logger.error("Async error: %s", msg)

    async def stop(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
        self._file_watcher.stop()
        await self._pool.terminate_all()
        if self._socket_client:
            await self._socket_client.close()
        if self._runner:
            await self._runner.cleanup()
        pid_file = self._config.config_dir / "daemon.pid"
        pid_file.unlink(missing_ok=True)
