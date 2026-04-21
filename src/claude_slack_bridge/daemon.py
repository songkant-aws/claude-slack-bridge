from __future__ import annotations

import asyncio
import json
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
        # YOLO: per-session auto-allow until daemon restart. Backs the
        # "YOLO" button on Slack approval cards.
        self._trusted_sessions: set[str] = set()
        self._cleanup_task: asyncio.Task | None = None
        # Event dedup cache — prevents duplicate Slack event processing
        self._seen = SeenCache()
        # Queued messages: session_id -> [text, ...]
        self._queued: dict[str, list[str]] = {}
        # Per-session mute level. Persisted so mute choices survive daemon restart.
        #   "sync" — user opted in: full TUI→Slack sync, permission requests
        #            post Slack buttons. (Set by /sync-on.)
        #   "ring" — ambient sync chatter silenced but permission requests
        #            still ring Slack. (Set by /sync-ring.)
        #   (absent) — default: full mute. Hook-side chatter and permission
        #            requests stay in the TUI. (Restored by /sync-off.)
        # PROCESS-mode stream events (Slack-originated sessions) bypass this
        # entirely — they always reach Slack regardless of mute level.
        self._muted_path = config.config_dir / "muted.json"
        self._mute_levels: dict[str, str] = self._load_muted()
        # Prompts forwarded from Slack→tmux — skip echo back via UserPromptSubmit hook
        # Capped at 50 entries; cleared entirely when exceeded (older entries are stale)
        self._forwarded_prompts: set[str] = set()
        # Pending approval messages: session_id -> msg_ts (for cleanup on TUI approval)
        self._pending_approval_msgs: dict[str, str] = {}
        # Live TodoWrite messages: session_id -> msg_ts (updated in place)
        self._todo_msgs: dict[str, str] = {}
        # Sessions that have been finalized this turn — JSONL watcher skips these
        self._finalized_sessions: set[str] = set()
        # Active reaction controllers per session (for TUI hook updates)
        self._reaction_controllers: dict[str, StatusReactionController] = {}
        # Conversation parser (Channel 2: JSONL file monitoring)
        self._conv_parser = ConversationParser()
        self._file_watcher = SessionFileWatcher(
            self._conv_parser, on_new_messages=self._on_jsonl_messages
        )

    # ── Muted-state persistence ──

    _VALID_LEVELS = {"sync", "ring"}

    def _load_muted(self) -> dict[str, str]:
        try:
            if self._muted_path.is_file():
                data = json.loads(self._muted_path.read_text())
                if isinstance(data, dict):
                    return {
                        k: v for k, v in data.items()
                        if isinstance(k, str) and v in self._VALID_LEVELS
                    }
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _save_muted(self) -> None:
        try:
            self._muted_path.parent.mkdir(parents=True, exist_ok=True)
            self._muted_path.write_text(
                json.dumps(self._mute_levels, sort_keys=True)
            )
        except OSError:
            logger.debug("Failed to persist muted dict", exc_info=True)

    def set_mute_level(self, session_id: str, level: str) -> None:
        """Record an explicit level ("sync" or "ring"). Removing a session
        from the dict (via clear_mute_level) drops it back to default full mute.
        """
        if level not in self._VALID_LEVELS:
            raise ValueError(f"invalid mute level: {level!r}")
        self._mute_levels[session_id] = level
        self._save_muted()

    def clear_mute_level(self, session_id: str) -> None:
        """Remove any explicit level → session falls back to default full mute."""
        self._mute_levels.pop(session_id, None)
        self._save_muted()

    def is_silenced(self, session_id: str) -> bool:
        """Gate ambient sync chatter. True unless session opted in via /sync-on."""
        return self._mute_levels.get(session_id) != "sync"

    def is_fully_muted(self, session_id: str) -> bool:
        """Gate permission-request handoff. True unless session is in sync or ring."""
        return self._mute_levels.get(session_id) not in ("sync", "ring")

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
                        # Clean up orphaned reaction controllers
                        rc = self._reaction_controllers.pop(session.session_id, None)
                        if rc:
                            asyncio.ensure_future(rc.finalize())
                        self._session_mgr.archive(session.session_id)
                        logger.info("Archived stale idle session %s", session.session_id)
                    elif session.mode == SessionMode.PROCESS.value and age > process_timeout:
                        rc = self._reaction_controllers.pop(session.session_id, None)
                        if rc:
                            asyncio.ensure_future(rc.finalize(error=True))
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
        basename = os.path.basename(self._config.work_dir.rstrip("/")) if self._config.work_dir else ""
        fallback = f"{basename} @ {session_id[:12]}" if basename else f"Session {session_id[:12]}"
        await self._slack.post_blocks(channel_id, blocks, fallback, thread_ts)

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
                channel_id, blocks,
                f"Resumed {session.session_id[:12]} — {session.session_name}",
                thread_ts,
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

    def _register_session(
        self, session_key: str, cwd: str, tmux_pane_id: str = ""
    ) -> Session:
        """Create a Session entry with no Slack binding yet.

        Every TUI-side hook that can arrive for an unknown session calls this
        so we can track origin/tmux/mode even before any Slack thread exists.
        Pair with `_ensure_slack_thread` to open the DM on demand.
        """
        session = self._session_mgr.create(
            session_id=session_key,
            session_name=f"TUI-{session_key[:12]}",
            channel_id="",
            thread_ts="",
            mode=SessionMode.HOOK,
        )
        session.cwd = cwd
        session.origin = "tui"
        session.tmux_pane_id = tmux_pane_id
        return session

    async def _ensure_slack_thread(self, session: Session) -> bool:
        """Open a DM thread for this session if one doesn't exist yet.

        Idempotent. Returns True once the session has a channel_id/thread_ts,
        False if Slack is unavailable or the user has no DM channel open
        with the bot.
        """
        if session.channel_id and session.thread_ts:
            return True
        if not self._slack:
            return False
        try:
            resp = await self._slack.web.conversations_list(types="im", limit=1)
            ims = resp.get("channels", [])
            if not ims:
                logger.warning("No DM channel found for lazy bind")
                return False
            dm_channel = ims[0]["id"]

            directory = session.cwd or self._config.work_dir
            blocks = build_session_header_blocks(
                session_id=session.session_id, directory=directory
            )
            basename = os.path.basename(directory.rstrip("/")) if directory else ""
            fallback = (
                f"{basename} @ {session.session_id[:12]}"
                if basename else f"Session {session.session_id[:12]}"
            )
            thread_ts = await self._slack.post_blocks(dm_channel, blocks, fallback)

            session.channel_id = dm_channel
            session.thread_ts = thread_ts
            # Keep the manager's reverse index in sync.
            self._session_mgr._thread_index[(dm_channel, thread_ts)] = session.session_id
            self._session_mgr._save()
            logger.info("Lazy-bound session %s → DM %s", session.session_id, dm_channel)
            return True
        except Exception:
            logger.error("Failed to lazy-bind session %s", session.session_id, exc_info=True)
            return False

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
        # Only remove the pid file if it still points at us; a racing
        # second start that SystemExit'd on port-in-use must not unlink
        # the file written by the first, healthy instance.
        pid_file = self._config.config_dir / "daemon.pid"
        try:
            if pid_file.is_file() and pid_file.read_text().strip() == str(os.getpid()):
                pid_file.unlink()
        except OSError:
            pass
