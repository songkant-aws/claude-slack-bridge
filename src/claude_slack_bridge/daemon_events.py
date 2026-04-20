"""Slack Socket Mode event handling.

Provides EventsMixin which is mixed into the Daemon class.
"""

from __future__ import annotations

import asyncio
import logging

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from claude_slack_bridge.reactions import StatusReactionController
from claude_slack_bridge.session_manager import SessionMode
from claude_slack_bridge.slack_formatter import (
    OPTIONS_ACTION_PREFIX,
    build_approval_resolved_blocks,
    build_session_header_blocks,
)
from claude_slack_bridge.tmux_controller import send_message_to_session

logger = logging.getLogger("claude_slack_bridge")


class EventsMixin:
    """Mixin providing Slack Socket Mode event handling."""

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

                event_ts = event.get("ts", "") or event.get("event_ts", "")
                if event_ts and self._seen.check_and_add(event_ts):
                    return

                if etype == "assistant_thread_started":
                    await self._handle_assistant_thread_started(event)
                elif etype == "assistant_thread_context_changed":
                    pass  # Acknowledged, no action needed
                elif etype == "app_mention":
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
                            await self._handle_dm(event, thread_ts)
                    elif channel.startswith("D"):
                        await self._handle_dm(event)
        except Exception:
            logger.error("Error handling socket event", exc_info=True)

    async def _handle_assistant_thread_started(self, event: dict) -> None:
        """Handle assistant_thread_started — activate the Assistant UI."""
        assistant_thread = event.get("assistant_thread", {})
        channel_id = assistant_thread.get("channel_id", "")
        thread_ts = assistant_thread.get("context", {}).get("channel_id", "") or assistant_thread.get("thread_ts", "")
        # The thread_ts is nested under assistant_thread
        if not thread_ts:
            thread_ts = event.get("assistant_thread", {}).get("thread_ts", "")
        if not channel_id or not thread_ts:
            return

        # Set suggested prompts to activate the Assistant UI
        try:
            import json as _json
            await self._slack.web.api_call(
                "assistant.threads.setSuggestedPrompts",
                params={
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "prompts": _json.dumps([
                        {"title": "Status", "message": "status"},
                        {"title": "Help", "message": "help"},
                    ]),
                },
            )
        except Exception:
            logger.debug("Failed to set suggested prompts", exc_info=True)

        # Set initial status
        await self._slack.set_thread_status(channel_id, thread_ts, "")

    async def _handle_mention(self, event: dict) -> None:
        channel_id = event.get("channel", "")
        text = event.get("text", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        msg_ts = event.get("ts", "")

        rc = StatusReactionController(self._slack, channel_id, msg_ts, asyncio.get_event_loop())
        await rc.set_phase("queued")

        prompt = text
        if self._bot_user_id:
            prompt = text.replace(f"<@{self._bot_user_id}>", "").strip()
        if not prompt:
            prompt = "Hello"

        parts = prompt.split()
        if len(parts) >= 2 and parts[0].lower() == "resume":
            await self._handle_resume_cmd(parts, channel_id, thread_ts)
            return

        active_count = len([s for s in self._session_mgr.list_active()
                           if s.mode == SessionMode.PROCESS.value])
        if active_count >= self._config.max_concurrent_sessions:
            await self._slack.post_text(
                channel_id,
                "\u26a0\ufe0f Max concurrent sessions ("
                + str(self._config.max_concurrent_sessions)
                + ") reached. Wait for a session to finish or use an existing thread.",
                thread_ts,
            )
            return

        await self._start_new_session(channel_id, thread_ts, prompt, reaction_controller=rc)

    async def _handle_dm(self, event: dict, thread_ts: str | None = None) -> None:
        channel_id = event.get("channel", "")
        text = event.get("text", "")
        msg_ts = event.get("ts", "")
        effective_ts = thread_ts or msg_ts

        rc = StatusReactionController(self._slack, channel_id, msg_ts, asyncio.get_event_loop())
        await rc.set_phase("queued")

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
                "\u26a0\ufe0f Max concurrent sessions ("
                + str(self._config.max_concurrent_sessions) + ") reached.",
                effective_ts,
            )
            return

        await self._start_new_session(channel_id, effective_ts, text, reaction_controller=rc)

    async def _handle_thread_reply(self, event: dict, thread_ts: str) -> None:
        channel_id = event.get("channel", "")
        text = event.get("text", "")
        msg_ts = event.get("ts", "")

        session = self._session_mgr.find_by_thread(channel_id, thread_ts)
        if not session:
            return

        rc = StatusReactionController(self._slack, channel_id, msg_ts, asyncio.get_event_loop())
        await rc.set_phase("queued")
        # Store so hooks (PostToolUse, Stop) can update phase
        self._reaction_controllers[session.session_id] = rc
        session.touch()

        lower = text.strip().lower()
        if lower in ("!stop", "stop"):
            cp = self._pool.get(session.session_id)
            if cp:
                await self._pool.terminate(session.session_id)
                pstate = self._progress.get(session.session_id, {})
                rc_ctrl: StatusReactionController | None = pstate.get("_reactions")
                if rc_ctrl:
                    asyncio.ensure_future(rc_ctrl.finalize(error=True))
                self._progress.pop(session.session_id, None)
                self._session_mgr.set_mode(session.session_id, SessionMode.IDLE)
                await self._slack.set_thread_status(channel_id, thread_ts, "")
                await self._slack.post_text(channel_id, "\u23f9\ufe0f _Stopped._", thread_ts)
            else:
                await self._slack.post_text(channel_id, "_No running process to stop._", thread_ts)
            return
        if lower == "yolo off":
            self._yolo_mode = False
            self._trusted_sessions.discard(session.session_id)
            await self._slack.post_text(channel_id, "\U0001f512 YOLO/Trust mode disabled", thread_ts)
            return
        if lower == "sync off":
            self.mute_session(session.session_id)
            await self._slack.post_text(channel_id, "\U0001f507 TUI sync muted for this session", thread_ts)
            return
        if lower == "sync on":
            self.unmute_session(session.session_id)
            await self._slack.post_text(channel_id, "\U0001f50a TUI sync resumed for this session", thread_ts)
            return

        # Check for pending approval — remind user instead of forwarding
        if session.session_id in self._pending_approval_msgs:
            await self._slack.post_text(
                channel_id,
                "\u26a0\ufe0f _Waiting for tool approval above. Please Approve or Reject before continuing._",
                thread_ts,
            )
            return

        if session.mode == SessionMode.PROCESS.value:
            await self._resume_process(session, text)
        elif session.origin == "tui":
            # TUI-originated session: try tmux send-keys first
            cwd = session.cwd or self._config.work_dir
            sent = await send_message_to_session(
                text, pane_id=session.tmux_pane_id, cwd=cwd,
            )
            if sent:
                if len(self._forwarded_prompts) >= 50:
                    self._forwarded_prompts.clear()
                self._forwarded_prompts.add(text.strip())
            else:
                # TUI gone — notify user and fall back to --print
                await self._slack.post_text(
                    channel_id,
                    "_TUI not available, starting Claude process..._",
                    thread_ts,
                )
                session.origin = "slack"
                await self._resume_process(session, text)
        else:
            # Slack-originated session: always use --print
            await self._resume_process(session, text)

    async def _handle_interactive(self, action: dict, payload: dict) -> None:
        action_id = action.get("action_id", "")
        value = action.get("value", "")
        channel_info = payload.get("channel", {})
        channel_id = channel_info.get("id", "")
        msg = payload.get("message", {})
        msg_ts = msg.get("ts", "")

        if action_id == "approve_tool":
            self._approval_mgr.resolve(value, "approved")
            if self._slack and channel_id and msg_ts:
                blocks = build_approval_resolved_blocks("Tool", "approved", value)
                try:
                    await self._slack.update_blocks(channel_id, msg_ts, blocks, text="\u2705 Approved")
                except Exception:
                    logger.debug("Failed to update approve message", exc_info=True)
        elif action_id == "reject_tool":
            self._approval_mgr.resolve(value, "rejected")
            if self._slack and channel_id and msg_ts:
                blocks = build_approval_resolved_blocks("Tool", "rejected", value)
                try:
                    await self._slack.update_blocks(channel_id, msg_ts, blocks, text="\U0001f6ab Rejected")
                except Exception:
                    logger.debug("Failed to update reject message", exc_info=True)
        elif action_id.startswith(OPTIONS_ACTION_PREFIX):
            thread_ts = msg.get("thread_ts", msg_ts)
            if channel_id and thread_ts:
                session = self._session_mgr.find_by_thread(channel_id, thread_ts)
                if session:
                    await self._resume_process(session, value)
                    if self._slack and msg_ts:
                        try:
                            await self._slack.web.chat_update(
                                channel=channel_id, ts=msg_ts,
                                text="\u2705 Selected: *" + value + "*", blocks=[],
                            )
                        except Exception:
                            logger.debug("Failed to update options message", exc_info=True)
        elif action_id == "trust_session":
            self._trusted_sessions.add(value)
            for req_id, state in list(self._approval_mgr._pending.items()):
                session = self._session_mgr.get(value)
                if session:
                    state.resolve("approved")
            if self._slack and channel_id and msg_ts:
                blocks = build_approval_resolved_blocks("Session", "trusted", value)
                try:
                    await self._slack.update_blocks(channel_id, msg_ts, blocks, text="\U0001f91d Trusted")
                except Exception:
                    logger.debug("Failed to update trust message", exc_info=True)
        elif action_id == "yolo_mode":
            self._yolo_mode = True
            for state in list(self._approval_mgr._pending.values()):
                state.resolve("approved")
            if self._slack and channel_id and msg_ts:
                blocks = build_approval_resolved_blocks("Global", "approved", value)
                try:
                    await self._slack.update_blocks(channel_id, msg_ts, blocks, text="\u2705 YOLO enabled")
                except Exception:
                    logger.debug("Failed to update YOLO message", exc_info=True)
        elif action_id == "takeover_session":
            session = self._session_mgr.get(value)
            if session:
                session.tui_active = 0
                await self._drain_queue(session)
                if self._slack and msg_ts:
                    try:
                        await self._slack.web.chat_update(
                            channel=channel_id, ts=msg_ts,
                            text="\u26a1 Slack has taken over session"
                        )
                    except Exception:
                        logger.debug("Failed to update takeover message", exc_info=True)
