"""HTTP API routes extracted from Daemon._create_http_app.

Usage:
    from claude_slack_bridge.daemon_http import create_http_app
    app = create_http_app(daemon)
"""

from __future__ import annotations

import logging
import time
import uuid

from aiohttp import web

logger = logging.getLogger("claude_slack_bridge")

from claude_slack_bridge.session_manager import SessionMode
from claude_slack_bridge.slack_formatter import (
    SLACK_MSG_LIMIT,
    build_approval_blocks,
    build_approval_resolved_blocks,
    build_session_header_blocks,
    build_tool_notification_blocks,
    build_user_prompt_blocks,
)

_SLACK_MAX_TEXT = SLACK_MSG_LIMIT


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
        return web.json_response({"status": "ok"})

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
                "muted": s.session_id in daemon._tui_sync_muted,
                "trusted": s.session_id in daemon._trusted_sessions,
            }
            cp = daemon._pool.get(s.session_id)
            if cp:
                info["pid"] = cp.process.pid
                info["process_started_at"] = cp.started_at
                if cp.init_at:
                    info["init_duration_ms"] = int((cp.init_at - cp.started_at) * 1000)
            result.append(info)
        return web.json_response({"yolo": daemon._yolo_mode, "sessions": result})

    @routes.post("/sessions/bind")
    async def bind_session(req: web.Request) -> web.Response:
        """Bind a TUI session to a new Slack DM thread."""
        payload = await req.json()
        session_id = payload.get("session_id", "")
        session_name = payload.get("name", session_id[:12])
        cwd = payload.get("cwd", daemon._config.work_dir)

        if not daemon._slack or not daemon._bot_user_id:
            return web.json_response({"error": "slack not connected"}, status=503)

        # Find or open DM with bot owner
        owner_id = payload.get("owner_id", "")
        if not owner_id:
            # Use first DM channel we know about
            resp = await daemon._slack.web.conversations_list(types="im", limit=1)
            ims = resp.get("channels", [])
            if ims:
                dm_channel = ims[0]["id"]
            else:
                return web.json_response({"error": "no DM channel found"}, status=404)
        else:
            resp = await daemon._slack.web.conversations_open(users=owner_id)
            dm_channel = resp["channel"]["id"]

        # Reuse existing thread if session already bound
        session = daemon._session_mgr.get(session_id)
        if session and session.thread_ts and session.channel_id == dm_channel:
            thread_ts = session.thread_ts
        else:
            # New session — post header as thread root
            blocks = build_session_header_blocks(session_id=session_id, directory=cwd)
            thread_ts = await daemon._slack.post_blocks(
                dm_channel, blocks, f"Session: {session_name}"
            )
            if not session:
                session = daemon._session_mgr.create(
                    session_id=session_id,
                    session_name=session_name,
                    channel_id=dm_channel,
                    thread_ts=thread_ts,
                    mode=SessionMode.IDLE,
                )
            else:
                session.channel_id = dm_channel
                session.thread_ts = thread_ts
                daemon._session_mgr._thread_index[(dm_channel, thread_ts)] = session.session_id
                daemon._session_mgr._save()
        session.cwd = cwd
        session.origin = "tui"

        return web.json_response({
            "ok": True,
            "channel_id": dm_channel,
            "thread_ts": thread_ts,
            "session_id": session_id,
        })

    @routes.post("/sessions/{session_id}/mute")
    async def mute_session(req: web.Request) -> web.Response:
        sid = req.match_info["session_id"]
        payload = await req.json()
        muted = payload.get("muted", True)
        if muted:
            daemon._tui_sync_muted.add(sid)
        else:
            daemon._tui_sync_muted.discard(sid)
        return web.json_response({"ok": True, "muted": muted})

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

            # Fast-path: YOLO mode — approve everything
            if daemon._yolo_mode:
                return web.Response(text="approved")

            # Fast-path: trusted session
            if session and session.session_id in daemon._trusted_sessions:
                return web.Response(text="approved")

            # Fast-path: safe tools (Read, Glob, Grep by default)
            if tool_name in daemon._config.auto_approve_tools:
                return web.Response(text="approved")

            # Need Slack connection to post approval buttons
            if not daemon._slack:
                return web.Response(text="approved")

            # Auto-bind session to a Slack DM if not yet registered
            if not session:
                session = await daemon._auto_bind_session(
                    session_key, payload.get("cwd", "")
                )
                if not session:
                    # Cannot reach Slack — fail open so TUI isn't blocked
                    return web.Response(text="approved")

            # Only PROCESS mode (daemon's own --print) needs Slack approval.
            # All other modes (HOOK, IDLE) are TUI sessions — TUI has its
            # own approval UI; blocking here causes double-approval.
            if session.mode != SessionMode.PROCESS.value:
                if session.session_id not in daemon._tui_sync_muted:
                    blocks = build_tool_notification_blocks(tool_name, tool_input)
                    await daemon._slack.post_blocks(
                        session.channel_id, blocks, f"\U0001f527 {tool_name}", session.thread_ts
                    )
                return web.Response(text="approved")

            # PROCESS mode: post approval buttons to the Slack thread
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
            state = daemon._approval_mgr.create(request_id)
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
        # Promote IDLE → HOOK when TUI hooks start arriving
        if session.mode == SessionMode.IDLE.value:
            daemon._session_mgr.set_mode(session.session_id, SessionMode.HOOK)
        # Promote origin to "tui" when TUI hooks arrive (e.g., user resumed
        # a Slack-originated session in TUI via `claude --resume`)
        if session.origin != "tui":
            session.origin = "tui"

        # JSONL watcher disabled — hooks (PostToolUse, Stop) now provide
        # all needed information; the watcher caused duplicate messages.

        # Sync TUI content to Slack (unless muted)
        if session.session_id not in daemon._tui_sync_muted:
            if hook_type == "user-prompt" and daemon._slack and session.channel_id:
                prompt_text = payload.get("prompt", "")
                # Skip Slack→tmux echo
                if prompt_text.strip() in daemon._forwarded_prompts:
                    daemon._forwarded_prompts.discard(prompt_text.strip())
                # Skip system messages (task-notification, system-reminder, etc.)
                elif prompt_text.strip().startswith("<") and any(
                    tag in prompt_text[:100]
                    for tag in ("task-notification", "system-reminder", "local-command")
                ):
                    pass
                else:
                    blocks = build_user_prompt_blocks(prompt_text)
                    await daemon._slack.post_blocks(
                        session.channel_id, blocks, "User prompt (TUI)", session.thread_ts
                    )
            elif hook_type == "post-tool-use" and daemon._slack and session.channel_id:
                tool_name = payload.get("tool_name", "")
                tool_input = payload.get("tool_input", {})

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
                        logger.debug("Failed to update approval message", exc_info=True)

                # Compact one-liner progress update
                if tool_name == "Bash":
                    detail = tool_input.get("command", "")[:80]
                elif tool_name in ("Read", "Write", "Edit", "Glob", "Grep"):
                    detail = tool_input.get("file_path", tool_input.get("pattern", ""))[:80]
                else:
                    detail = tool_name
                await daemon._update_progress(
                    session, f"\U0001fac6 `{tool_name}` {detail}"
                )
            elif hook_type == "stop" and daemon._slack and session.channel_id:
                # PROCESS mode already finalizes via _on_stream_event result.
                # HOOK/IDLE modes are TUI sessions — finalize from hook.
                if session.mode != SessionMode.PROCESS.value:
                    response_text = payload.get("response", "")
                    logger.info(
                        "Stop hook: session=%s mode=%s response_len=%d",
                        session.session_id[:12], session.mode, len(response_text),
                    )
                    if response_text:
                        await daemon._finalize_progress(session, response_text)

        if hook_type == "stop":
            # TUI exited — drain queued Slack messages
            await daemon._drain_queue(session)

        return web.Response(text="ok")

    @routes.post("/hooks/session-start")
    async def hook_session_start(req: web.Request) -> web.Response:
        payload = await req.json()
        session_key = payload.get("session_key", "")
        cwd = payload.get("cwd", "")

        session = daemon._session_mgr.get(session_key)
        if not session:
            session = await daemon._auto_bind_session(session_key, cwd)
        if session:
            session.touch()
            session.tui_active = time.time()
            session.cwd = cwd or session.cwd
            daemon._session_mgr.set_mode(session_key, SessionMode.HOOK)
            if daemon._slack and session.channel_id and session.session_id not in daemon._tui_sync_muted:
                await daemon._slack.post_text(
                    session.channel_id,
                    "▶️ _Session started_",
                    session.thread_ts,
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
            daemon._file_watcher.unwatch(session_key)
            if daemon._slack and session.channel_id and session.session_id not in daemon._tui_sync_muted:
                await daemon._slack.post_text(
                    session.channel_id,
                    "⏹️ _Session ended_",
                    session.thread_ts,
                )
            daemon._session_mgr.set_mode(session_key, SessionMode.IDLE)
            daemon._progress.pop(session_key, None)
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

        if daemon._slack and session.channel_id and session.session_id not in daemon._tui_sync_muted:
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
            if daemon._slack and session.channel_id and session.session_id not in daemon._tui_sync_muted:
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
            if daemon._slack and session.channel_id and session.session_id not in daemon._tui_sync_muted:
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

        # Fast-path: YOLO mode
        if daemon._yolo_mode:
            return web.Response(text="approved")

        session = daemon._session_mgr.get(session_key)

        # Fast-path: trusted session
        if session and session.session_id in daemon._trusted_sessions:
            return web.Response(text="approved")

        # Fast-path: safe tools
        if tool_name in daemon._config.auto_approve_tools:
            return web.Response(text="approved")

        if not daemon._slack:
            return web.Response(text="approved")

        # Auto-bind if needed
        if not session:
            session = await daemon._auto_bind_session(session_key, cwd)
            if not session:
                return web.Response(text="approved")

        session.touch()
        session.tui_active = time.time()

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
        state = daemon._approval_mgr.create(request_id)
        result = await state.wait(
            timeout=daemon._config.approval_timeout_secs
        )
        daemon._approval_mgr.cleanup(request_id)
        daemon._pending_approval_msgs.pop(session.session_id, None)

        # Clear thread status after approval
        await daemon._slack.set_thread_status(
            session.channel_id, session.thread_ts, ""
        )

        return web.Response(
            text="approved" if result == "approved" else "rejected"
        )

    app = web.Application()
    app.router.add_routes(routes)
    return app
