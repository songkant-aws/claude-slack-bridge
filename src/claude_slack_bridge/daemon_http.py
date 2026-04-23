"""HTTP API routes extracted from Daemon._create_http_app.

Usage:
    from claude_slack_bridge.daemon_http import create_http_app
    app = create_http_app(daemon)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid

from aiohttp import web

logger = logging.getLogger("claude_slack_bridge")

from claude_slack_bridge.session_manager import SessionMode
from claude_slack_bridge.slack_formatter import (
    SLACK_MSG_LIMIT,
    build_approval_blocks,
    build_approval_resolved_blocks,
    build_tool_notification_blocks,
    build_user_prompt_blocks,
)

_SLACK_MAX_TEXT = SLACK_MSG_LIMIT

# Wrapper tags Claude Code prepends to user prompts: system reminders (e.g.
# plan-mode notices), slash-command metadata, and local-command output blocks.
# Stripping these *leading* blocks recovers any real user text that follows,
# so a plan-mode prompt like
#     "<system-reminder>Plan mode is active...</system-reminder>\n\nfix the bug"
# still syncs "fix the bug" to Slack instead of being dropped wholesale.
_WRAPPER_TAG_RE = re.compile(
    r"^\s*<(system-reminder|task-notification|command-name|command-message|"
    r"command-args|local-command-[\w-]+)>.*?</\1>\s*",
    re.DOTALL,
)


def _strip_wrapper_blocks(text: str) -> str:
    """Peel off leading <system-reminder>/<command-*>/<local-command-*> blocks.

    Returns the remainder stripped of surrounding whitespace. If the whole
    payload is wrapper-only, returns an empty string and the caller should
    treat it as a synthetic prompt (don't post to Slack).
    """
    prev = None
    while prev != text:
        prev = text
        text = _WRAPPER_TAG_RE.sub("", text, count=1)
    return text.strip()


async def _maybe_warn_version_mismatch(
    daemon, channel_id: str, thread_ts: str, plugin_version: str
) -> None:
    """Post a one-time warning when the TUI-side plugin and the daemon
    were installed from different versions of the repo. Repeats only if
    the mismatched version changes (so the nag doesn't fire every session
    start) and suppresses itself when either side omits a version.
    """
    from claude_slack_bridge import __version__ as daemon_version

    if not plugin_version or not daemon_version:
        return
    if plugin_version == daemon_version:
        return
    # Only warn once per distinct mismatch pair so every session-start
    # doesn't spam the thread.
    last = getattr(daemon, "_last_version_warning", None)
    pair = (plugin_version, daemon_version)
    if last == pair:
        return
    daemon._last_version_warning = pair
    await daemon._slack.post_text(
        channel_id,
        (
            f"⚠️ Version mismatch: plugin `{plugin_version}` ↔ daemon `{daemon_version}`. "
            "Run `claude-slack-bridge update` and restart the TUI to resync."
        ),
        thread_ts,
    )


def _format_todos(todos: list[dict]) -> str:
    """Render a TodoWrite todo list into a Slack-friendly checklist."""
    if not todos:
        return "📋 _(empty todo list)_"
    icons = {"completed": "✅", "in_progress": "🔄", "pending": "⏳"}
    lines = ["📋 *Progress*"]
    for t in todos:
        status = t.get("status", "pending")
        icon = icons.get(status, "•")
        # activeForm for in_progress (e.g. "Running tests"), plain content otherwise
        text = t.get("activeForm") if status == "in_progress" else t.get("content", "")
        if not text:
            text = t.get("content", "")
        lines.append(f"{icon} {text}")
    return "\n".join(lines)[:_SLACK_MAX_TEXT]


async def _post_or_update_todos(daemon, session, todos: list[dict]) -> None:
    """Post a new todo message or update the existing one in place."""
    text = _format_todos(todos)
    existing_ts = daemon._todo_msgs.get(session.session_id)
    if existing_ts:
        try:
            await daemon._slack.update_text(session.channel_id, existing_ts, text)
            return
        except Exception:
            logger.debug("Todo update failed, posting new message", exc_info=True)
            daemon._todo_msgs.pop(session.session_id, None)
    try:
        ts = await daemon._slack.post_text(session.channel_id, text, session.thread_ts)
        daemon._todo_msgs[session.session_id] = ts
    except Exception:
        logger.debug("Todo post failed", exc_info=True)


def _read_last_turn_from_jsonl(conv_parser, session_id: str, cwd: str) -> str:
    """Read all assistant text from the last turn in the JSONL file.

    Claude Island pattern: JSONL is the single source of truth.
    Returns all assistant text blocks after the last user message.
    """
    if not cwd:
        return ""
    try:
        # Force a fresh incremental read
        messages = conv_parser.parse_incremental(session_id, cwd)
        # Get all messages including previously parsed ones
        all_msgs = conv_parser.get_all_messages(session_id)
        if not all_msgs:
            return ""

        # Find last user message index, collect all assistant text after it
        last_user_idx = -1
        for i, msg in enumerate(all_msgs):
            if msg.role == "user":
                last_user_idx = i

        parts: list[str] = []
        start = last_user_idx + 1 if last_user_idx >= 0 else 0
        for msg in all_msgs[start:]:
            if msg.role == "assistant" and msg.text:
                # Prefix each block with ● so finalized text matches the
                # streaming progress (see daemon_stream._on_jsonl_messages)
                # — otherwise Slack readers get a wall of text while the
                # TUI shows neat bullets between each reasoning step.
                parts.append("● " + msg.text)
        return "\n\n".join(parts)
    except Exception:
        return ""


def _read_recent_assistant_text(conv_parser, session_id: str, cwd: str) -> str:
    """Read the most recent assistant text from JSONL that hasn't been shown yet.

    Called on PostToolUse to surface intermediate reasoning text in real-time.
    Returns the last assistant text block before the most recent tool_use.
    """
    if not cwd:
        return ""
    try:
        conv_parser.parse_incremental(session_id, cwd)
        all_msgs = conv_parser.get_all_messages(session_id)
        if not all_msgs:
            return ""

        # Walk backwards: find the last assistant text before the last tool_use
        for i in range(len(all_msgs) - 1, -1, -1):
            msg = all_msgs[i]
            if msg.role == "assistant" and msg.text:
                return msg.text
            if msg.role == "user":
                break  # Don't go past the current turn
        return ""
    except Exception:
        return ""


def create_http_app(daemon) -> web.Application:
    """Build an aiohttp Application with all daemon HTTP routes.

    Parameters
    ----------
    daemon:
        A ``Daemon`` instance whose attributes and methods are accessed
        via ``daemon._xxx`` in place of the original ``self._xxx``.
    """
    routes = web.RouteTableDef()

    @routes.get("/health")
    async def health(req: web.Request) -> web.Response:
        from claude_slack_bridge import __version__
        return web.json_response({"status": "ok", "version": __version__})

    @routes.get("/sessions")
    async def list_sessions(req: web.Request) -> web.Response:
        import time as _t
        now = _t.time()
        active = daemon._session_mgr.list_active()
        result = []
        for s in active:
            info: dict = {
                "session_id": s.session_id,
                "name": s.session_name,
                "mode": s.mode,
                "channel_id": s.channel_id,
                "thread_ts": s.thread_ts,
                "cwd": s.cwd,
                "created_at": s.created_at,
                "last_active": s.last_active,
                "age_secs": int(now - s.created_at),
                "idle_secs": int(now - s.last_active),
                "mute_level": daemon._mute_levels.get(s.session_id),
                "trusted": s.session_id in daemon._trusted_sessions,
            }
            cp = daemon._pool.get(s.session_id)
            if cp:
                info["pid"] = cp.process.pid
                info["process_started_at"] = cp.started_at
                if cp.init_at:
                    info["init_duration_ms"] = int((cp.init_at - cp.started_at) * 1000)
            result.append(info)
        return web.json_response({"sessions": result})

    @routes.post("/sessions/bind")
    async def bind_session(req: web.Request) -> web.Response:
        """Bind a TUI session to a Slack DM thread (called by /sync-on)."""
        payload = await req.json()
        session_id = payload.get("session_id", "")
        session_name = payload.get("name", f"TUI-{session_id[:12]}")
        cwd = payload.get("cwd", daemon._config.work_dir)
        tmux_pane_id = payload.get("tmux_pane_id", "")

        if not daemon._slack or not daemon._bot_user_id:
            return web.json_response({"error": "slack not connected"}, status=503)

        session = daemon._session_mgr.get(session_id)
        if not session:
            session = daemon._register_session(session_id, cwd, tmux_pane_id=tmux_pane_id)
        session.session_name = session_name
        session.cwd = cwd
        session.origin = "tui"
        if tmux_pane_id:
            session.tmux_pane_id = tmux_pane_id

        if not await daemon._ensure_slack_thread(session):
            return web.json_response({"error": "no DM channel found"}, status=404)

        return web.json_response({
            "ok": True,
            "channel_id": session.channel_id,
            "thread_ts": session.thread_ts,
            "session_id": session_id,
        })

    @routes.post("/sessions/{session_id}/mute")
    async def mute_session(req: web.Request) -> web.Response:
        """Set the session's mute level.

        Payload: {"level": "sync" | "ring" | "none"}
          sync — explicit opt-in to TUI→Slack sync (from /sync-on)
          ring — silence ambient chatter but keep Slack approvals (/sync-ring)
          none — drop back to default full mute (/sync-off, or unset state)
        """
        sid = req.match_info["session_id"]
        payload = await req.json()
        level = payload.get("level")
        if level == "none":
            daemon.clear_mute_level(sid)
            return web.json_response({"ok": True, "level": None})
        if level in ("sync", "ring"):
            daemon.set_mute_level(sid, level)
            # Ring needs a Slack thread eventually (for approvals). Creating
            # it here — rather than on the first permission-request — means
            # the user sees the thread appear right when they opt in.
            if level == "ring":
                session = daemon._session_mgr.get(sid)
                if session:
                    await daemon._ensure_slack_thread(session)
            return web.json_response({"ok": True, "level": level})
        return web.json_response(
            {"ok": False, "error": "level must be one of: sync, ring, none"},
            status=400,
        )

    @routes.post("/hooks/{hook_type}")
    async def hook_handler(req: web.Request) -> web.Response:
        hook_type = req.match_info["hook_type"]
        payload = await req.json()
        session_key = payload.get("session_key", "")
        logger.info("Hook received: %s session=%s", hook_type, session_key[:12] if session_key else "?")

        session = daemon._session_mgr.get(session_key)

        # pre-tool-use can arrive from unbound TUI sessions — handle
        # before the session-required checks below (Issue #2).
        if hook_type == "pre-tool-use":
            tool_name = payload.get("tool_name", "")
            tool_input = payload.get("tool_input", {})

            # Fast-path: YOLO / trusted session (per-session auto-allow)
            if session and session.session_id in daemon._trusted_sessions:
                return web.Response(text="approved")

            # Fast-path: safe tools (Read, Glob, Grep by default)
            if tool_name in daemon._config.auto_approve_tools:
                return web.Response(text="approved")

            # Unbound TUI sessions or missing Slack: auto-approve. Lazy bind
            # happens from /hooks/permission-request or /sessions/bind, not here.
            if not daemon._slack or not session:
                return web.Response(text="approved")

            # Only PROCESS mode (daemon's own --print) needs Slack approval.
            # All other modes (HOOK, IDLE) are TUI sessions — TUI has its
            # own approval UI; blocking here causes double-approval.
            if session.mode != SessionMode.PROCESS.value:
                return web.Response(text="approved")

            # PROCESS mode: post approval buttons to the Slack thread.
            # Seal the in-flight progress message first so the buttons
            # land below the current stream; otherwise later chat_update
            # calls on the progress msg re-order it visually above the
            # approval and users think the stream has stalled.
            await daemon._seal_progress(session)
            await daemon._slack.set_thread_status(
                session.channel_id, session.thread_ts,
                f"Waiting for approval \u2014 {tool_name}..."
            )
            request_id = str(uuid.uuid4())
            blocks = build_approval_blocks(
                tool_name=tool_name,
                tool_input=tool_input,
                session_id=session.session_id,
                session_name=session.session_name,
                request_id=request_id,
            )
            await daemon._slack.post_blocks(
                session.channel_id,
                blocks,
                f"\U0001f510 Approve {tool_name}?",
                session.thread_ts,
            )

            # Block until user clicks Approve/Reject or timeout
            state = daemon._approval_mgr.create(
                request_id,
                tool_name=tool_name,
                tool_input=tool_input,
                cwd=payload.get("cwd", "") or session.cwd,
                session_id=session.session_id,
            )
            result = await state.wait(
                timeout=daemon._config.approval_timeout_secs
            )
            daemon._approval_mgr.cleanup(request_id)
            await daemon._slack.set_thread_status(
                session.channel_id, session.thread_ts, ""
            )

            return web.Response(
                text="approved" if result == "approved" else "rejected"
            )

        # Other hooks require an existing session
        if not session:
            return web.json_response({"error": "unknown session"}, status=404)

        # TUI hook arrived — update session state
        session.touch()
        session.tui_active = time.time()
        # Refresh tmux pane binding — handles --resume in a new pane
        pane_id = payload.get("tmux_pane_id", "")
        if pane_id and pane_id != session.tmux_pane_id:
            session.tmux_pane_id = pane_id
        # Promote IDLE → HOOK when TUI hooks start arriving
        if session.mode == SessionMode.IDLE.value:
            daemon._session_mgr.set_mode(session.session_id, SessionMode.HOOK)
        # Promote origin to "tui" when TUI hooks arrive (e.g., user resumed
        # a Slack-originated session in TUI via `claude --resume`)
        if session.origin != "tui":
            session.origin = "tui"

        # Sync TUI content to Slack (unless silenced by any mute level).
        # Permission-request has its own gate (is_fully_muted) downstream.
        if not daemon.is_silenced(session.session_id):
            if hook_type == "user-prompt" and daemon._slack and session.channel_id:
                # New turn starting — clear stale state, start JSONL watcher
                daemon._finalized_sessions.discard(session.session_id)
                daemon._progress.pop(session.session_id, None)
                # Start JSONL watcher (Claude Island pattern: hooks are lifecycle
                # signals, watcher is the content source)
                cwd = payload.get("cwd", "") or session.cwd
                if cwd:
                    daemon._file_watcher.watch(session.session_id, cwd)
                # Set thread status — activates glowing name
                await daemon._slack.set_thread_status(
                    session.channel_id, session.thread_ts, "is working on your request"
                )
                prompt_text = payload.get("prompt", "")
                stripped = prompt_text.strip()
                # Skip Slack→tmux echo (forwarded from Slack, would be duplicate)
                if stripped in daemon._forwarded_prompts:
                    daemon._forwarded_prompts.discard(stripped)
                else:
                    # Peel off leading <system-reminder>/<command-*> blocks
                    # that Claude Code prepends (e.g. plan-mode notices). Post
                    # whatever real user text survives; if nothing survives,
                    # it was a pure wrapper — skip it.
                    user_text = _strip_wrapper_blocks(stripped)
                    if user_text:
                        await daemon._slack.post_text(
                            session.channel_id,
                            f"\U0001f4ac *User:* {user_text[:3000]}",
                            session.thread_ts,
                        )
            elif hook_type == "post-tool-use" and daemon._slack and session.channel_id:
                tool_name = payload.get("tool_name", "")
                tool_input = payload.get("tool_input", {})

                # Update phase-aware reaction + thread status
                rc = daemon._reaction_controllers.get(session.session_id)
                if rc:
                    from claude_slack_bridge.reactions import tool_to_phase
                    phase = tool_to_phase(tool_name)
                    asyncio.ensure_future(rc.set_phase(phase))
                    rc.on_progress()
                await daemon._slack.set_thread_status(
                    session.channel_id, session.thread_ts, f"is using {tool_name}"
                )

                # If there's a pending approval message, replace it with result
                pending_ts = daemon._pending_approval_msgs.pop(session.session_id, None)
                if pending_ts:
                    try:
                        blocks = build_approval_resolved_blocks(tool_name, "approved", "")
                        await daemon._slack.update_blocks(
                            session.channel_id, pending_ts, blocks,
                            text="\u2705 Approved in TUI"
                        )
                    except Exception:
                        logger.warning("Failed to update approval message", exc_info=True)

                # TodoWrite gets its own persistent, in-place updated message
                if tool_name == "TodoWrite":
                    await _post_or_update_todos(daemon, session, tool_input.get("todos", []))
                    return web.Response(text="ok")

                # Tool status lines are rendered by the JSONL watcher
                # (_on_jsonl_messages) as the single content source. The
                # hook's job here is limited to status side-effects —
                # reaction phase, thread status, pending-approval cleanup
                # — so we don't double-append the tool line.
            elif hook_type == "stop" and daemon._slack and session.channel_id:
                # Stop JSONL watcher FIRST — prevents race with finalize
                daemon._file_watcher.unwatch(session.session_id)

                # Forget the per-turn todo message so the next turn starts fresh
                daemon._todo_msgs.pop(session.session_id, None)

                # Clear thread status (stop glowing)
                await daemon._slack.set_thread_status(
                    session.channel_id, session.thread_ts, ""
                )
                # Finalize reaction controller (eyes → lobster/error)
                rc = daemon._reaction_controllers.pop(session.session_id, None)
                if rc:
                    asyncio.ensure_future(rc.finalize(error=False))

                # PROCESS mode already finalizes via _on_stream_event result.
                # HOOK/IDLE modes: read JSONL for full turn, overwrite progress.
                if session.mode != SessionMode.PROCESS.value:
                    cwd = payload.get("cwd", "") or session.cwd
                    full_text = _read_last_turn_from_jsonl(
                        daemon._conv_parser, session.session_id, cwd
                    )
                    if not full_text:
                        # Fallback to hook payload
                        full_text = payload.get("response", "")
                    if full_text:
                        await daemon._finalize_progress(session, full_text)

        if hook_type == "stop":
            # TUI exited — drain queued Slack messages
            await daemon._drain_queue(session)

        return web.Response(text="ok")

    @routes.post("/hooks/session-start")
    async def hook_session_start(req: web.Request) -> web.Response:
        payload = await req.json()
        session_key = payload.get("session_key", "")
        cwd = payload.get("cwd", "")
        pane_id = payload.get("tmux_pane_id", "")
        plugin_version = payload.get("plugin_version", "")

        session = daemon._session_mgr.get(session_key)
        if not session:
            session = daemon._register_session(session_key, cwd, tmux_pane_id=pane_id)
        session.touch()
        session.tui_active = time.time()
        session.cwd = cwd or session.cwd
        if pane_id:
            session.tmux_pane_id = pane_id
        daemon._session_mgr.set_mode(session_key, SessionMode.HOOK)

        # Only chatter into Slack when the user has opted in (sync mode).
        # Default mute keeps new TUI sessions out of the DM entirely.
        if daemon._slack and not daemon.is_silenced(session.session_id):
            if await daemon._ensure_slack_thread(session):
                await daemon._slack.post_text(
                    session.channel_id,
                    "▶️ _Session started_",
                    session.thread_ts,
                )
                await _maybe_warn_version_mismatch(
                    daemon, session.channel_id, session.thread_ts, plugin_version,
                )
        return web.Response(text="ok")

    @routes.post("/hooks/session-end")
    async def hook_session_end(req: web.Request) -> web.Response:
        payload = await req.json()
        session_key = payload.get("session_key", "")

        session = daemon._session_mgr.get(session_key)
        if session:
            session.touch()
            session.tui_active = 0
            # Pane is about to close — clear so Slack→TUI forwarding falls
            # back to cwd/--resume instead of send-keys to a stale pane.
            session.tmux_pane_id = ""
            daemon._file_watcher.unwatch(session_key)
            if daemon._slack and session.channel_id and not daemon.is_silenced(session.session_id):
                await daemon._slack.post_text(
                    session.channel_id,
                    "⏹️ _Session ended_",
                    session.thread_ts,
                )
            daemon._session_mgr.set_mode(session_key, SessionMode.IDLE)
            daemon._progress.pop(session_key, None)
            daemon._todo_msgs.pop(session_key, None)
        return web.Response(text="ok")

    @routes.post("/hooks/notification")
    async def hook_notification(req: web.Request) -> web.Response:
        payload = await req.json()
        session_key = payload.get("session_key", "")
        notification_type = payload.get("notification_type", "")
        message = payload.get("message", "")

        session = daemon._session_mgr.get(session_key)
        if not session:
            return web.Response(text="ok")
        session.touch()

        # Skip permission_prompt — handled by PreToolUse
        if notification_type == "permission_prompt":
            return web.Response(text="ok")

        if daemon._slack and session.channel_id and not daemon.is_silenced(session.session_id):
            if notification_type == "idle_prompt":
                await daemon._slack.post_text(
                    session.channel_id,
                    "⏸️ _Waiting for input..._",
                    session.thread_ts,
                )
            elif message:
                await daemon._slack.post_text(
                    session.channel_id,
                    f"🔔 {message[:_SLACK_MAX_TEXT]}",
                    session.thread_ts,
                )
        return web.Response(text="ok")

    @routes.post("/hooks/subagent-stop")
    async def hook_subagent_stop(req: web.Request) -> web.Response:
        payload = await req.json()
        session_key = payload.get("session_key", "")

        session = daemon._session_mgr.get(session_key)
        if session:
            session.touch()
            if daemon._slack and session.channel_id and not daemon.is_silenced(session.session_id):
                await daemon._update_progress(session, "🤖 _Subagent completed_")
        return web.Response(text="ok")

    @routes.post("/hooks/pre-compact")
    async def hook_pre_compact(req: web.Request) -> web.Response:
        payload = await req.json()
        session_key = payload.get("session_key", "")
        compact_type = payload.get("compact_type", "auto")

        session = daemon._session_mgr.get(session_key)
        if session:
            session.touch()
            if daemon._slack and session.channel_id and not daemon.is_silenced(session.session_id):
                await daemon._slack.post_text(
                    session.channel_id,
                    f"📦 _Compacting context ({compact_type})..._",
                    session.thread_ts,
                )
        return web.Response(text="ok")

    @routes.post("/hooks/permission-request")
    async def hook_permission_request(req: web.Request) -> web.Response:
        """Handle PermissionRequest from TUI — block until Slack approval."""
        payload = await req.json()
        session_key = payload.get("session_key", "")
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input", {})
        cwd = payload.get("cwd", "")
        logger.info(
            "Hook received: permission-request session=%s tool=%s",
            session_key[:12] if session_key else "?", tool_name,
        )

        session = daemon._session_mgr.get(session_key)

        # Fast-path: YOLO / trusted session
        if session and session.session_id in daemon._trusted_sessions:
            return web.Response(text="approved")

        # Fast-path: safe tools
        if tool_name in daemon._config.auto_approve_tools:
            return web.Response(text="approved")

        if not daemon._slack:
            return web.Response(text="approved")

        # Register session (no Slack thread yet) so we can check mute level.
        if not session:
            session = daemon._register_session(
                session_key, cwd,
                tmux_pane_id=payload.get("tmux_pane_id", ""),
            )

        session.touch()
        session.tui_active = time.time()
        pane_id = payload.get("tmux_pane_id", "")
        if pane_id and pane_id != session.tmux_pane_id:
            session.tmux_pane_id = pane_id

        # Full mute: hand approval back to CC's native TUI dialog by
        # returning an empty body. The hook script's "unknown decision"
        # branch falls through so CC shows its own prompt. No Slack
        # thread gets created in this path.
        if daemon.is_fully_muted(session.session_id):
            return web.Response(text="")

        # Ring/sync mute: lazy-create the DM thread now so approval
        # buttons have somewhere to land.
        if not await daemon._ensure_slack_thread(session):
            return web.Response(text="approved")

        # Seal the in-flight progress message so approval buttons land
        # below the current stream (see PROCESS-mode site above for why).
        await daemon._seal_progress(session)

        # Thread status: waiting for approval
        await daemon._slack.set_thread_status(
            session.channel_id, session.thread_ts,
            f"Waiting for approval \u2014 {tool_name}..."
        )

        # Post approval buttons
        request_id = str(uuid.uuid4())
        blocks = build_approval_blocks(
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=session.session_id,
            session_name=session.session_name,
            request_id=request_id,
        )
        approval_msg_ts = await daemon._slack.post_blocks(
            session.channel_id,
            blocks,
            f"\U0001f510 Approve {tool_name}?",
            session.thread_ts,
        )
        # Track for cleanup if TUI approves before Slack click
        daemon._pending_approval_msgs[session.session_id] = approval_msg_ts

        # Block until Slack button click or timeout
        state = daemon._approval_mgr.create(
            request_id,
            tool_name=tool_name,
            tool_input=tool_input,
            cwd=cwd or session.cwd,
            session_id=session.session_id,
        )
        result = await state.wait(
            timeout=daemon._config.approval_timeout_secs
        )
        daemon._approval_mgr.cleanup(request_id)
        daemon._pending_approval_msgs.pop(session.session_id, None)

        # Clear thread status after approval
        await daemon._slack.set_thread_status(
            session.channel_id, session.thread_ts, ""
        )

        if result == "trusted":
            return web.json_response({
                "decision": "trusted",
                "tool_name": state.trust_tool_name,
                "rule_content": state.trust_rule_content,
                "destination": state.trust_destination,
            })
        return web.json_response({"decision": result})

    app = web.Application()
    app.router.add_routes(routes)
    return app
