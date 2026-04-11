"""Stream event processing and progress message management.

Provides StreamMixin which is mixed into the Daemon class.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from claude_slack_bridge.reactions import StatusReactionController, tool_to_phase
from claude_slack_bridge.slack_formatter import (
    SLACK_MSG_LIMIT,
    build_options_blocks,
    build_permission_denied_blocks,
    extract_options,
    md_to_mrkdwn,
    split_message,
    strip_thinking_tags,
)
from claude_slack_bridge.stream_parser import StreamEvent

if TYPE_CHECKING:
    from claude_slack_bridge.session_manager import Session

logger = logging.getLogger("claude_slack_bridge")

_SLACK_MAX_TEXT = SLACK_MSG_LIMIT
_EDIT_INTERVAL = 1.0   # Minimum seconds between Slack message edits
_CURSOR = " \u25cd"     # Streaming cursor character


class StreamMixin:
    """Mixin providing stream event processing and progress message management."""

    # ── Progress message (single message, overwritten by final result) ──

    async def _update_progress(self, session: Session, line: str, replace: bool = False) -> None:
        """Update progress message.

        If *replace* is True, the last line is replaced instead of appended.
        Used for tool status so only the current tool shows (no accumulation).
        """
        sid = session.session_id
        now = time.time()
        if sid not in self._progress:
            msg_ts = await self._slack.post_text(
                session.channel_id, line + _CURSOR, session.thread_ts
            )
            self._progress[sid] = {"msg_ts": msg_ts, "last_update": now, "lines": [line]}
        else:
            state = self._progress[sid]
            if replace and state["lines"]:
                state["lines"][-1] = line
            else:
                state["lines"].append(line)
            display = state["lines"][-3:]
            if now - state["last_update"] >= _EDIT_INTERVAL:
                text = "\n".join(display) + _CURSOR
                try:
                    await self._slack.web.chat_update(
                        channel=session.channel_id, ts=state["msg_ts"], text=text[:_SLACK_MAX_TEXT]
                    )
                    state["last_update"] = now
                except Exception:
                    logger.debug("Failed to update progress message", exc_info=True)

    async def _finalize_progress(self, session: Session, final_text: str) -> None:
        """Post final result. Keeps progress message as context if it has content."""
        sid = session.session_id

        cleaned, _thinking = strip_thinking_tags(final_text)
        cleaned, choices = extract_options(cleaned)
        display = md_to_mrkdwn(cleaned)

        if sid in self._progress:
            state = self._progress.pop(sid)
            lines = state.get("lines", [])
            msg_ts = state.get("msg_ts")

            if lines and msg_ts:
                # Progress has content (tool calls, intermediate text) — keep it
                # as context, post final response as new message
                try:
                    summary = "\n".join(lines[-8:])
                    await self._slack.web.chat_update(
                        channel=session.channel_id, ts=msg_ts,
                        text=summary[:_SLACK_MAX_TEXT],
                    )
                except Exception:
                    pass
                # Post final response as new message
                chunks = split_message(display)
                for chunk in chunks:
                    await self._slack.post_text(
                        session.channel_id, chunk, session.thread_ts
                    )
            elif msg_ts:
                # Progress is empty placeholder — overwrite with response
                try:
                    chunks = split_message(display)
                    await self._slack.web.chat_update(
                        channel=session.channel_id, ts=msg_ts, text=chunks[0]
                    )
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
            chunks = split_message(display)
            for chunk in chunks:
                await self._slack.post_text(
                    session.channel_id, chunk, session.thread_ts
                )

        # Mark finalized so JSONL watcher won't duplicate
        self._finalized_sessions.add(sid)

        if choices:
            blocks = build_options_blocks(choices)
            await self._slack.post_blocks(
                session.channel_id, blocks, "Choose an option", session.thread_ts
            )

    # ── JSONL file monitoring (Channel 2) ──

    async def _on_jsonl_messages(self, session_id: str, messages) -> None:
        """Called when new messages are found in JSONL file by the file watcher."""
        from claude_slack_bridge.session_manager import SessionMode

        session = self._session_mgr.get(session_id)
        if not session or not self._slack:
            return
        if session.session_id in self._tui_sync_muted:
            return
        if session.mode != SessionMode.HOOK.value:
            return
        # Skip if this turn was already finalized by Stop hook
        if session.session_id in self._finalized_sessions:
            return

        for msg in messages:
            if msg.role == "assistant" and msg.text:
                await self._update_progress(session, "_" + msg.text[:200] + "_")
            elif msg.role == "tool_use":
                detail = ""
                if msg.tool_name == "Bash":
                    detail = msg.tool_input.get("command", "")[:60]
                elif msg.tool_name in ("Read", "Write", "Edit"):
                    detail = msg.tool_input.get("file_path", "")[:60]
                else:
                    detail = msg.tool_name
                await self._update_progress(session, "\U0001fac6 `" + msg.tool_name + "` " + detail, replace=True)

    # ── Stream event handler (PROCESS mode) ──

    async def _on_stream_event(self, session_id: str, evt: StreamEvent) -> None:
        session = self._session_mgr.get(session_id)
        if not session or not self._slack:
            return
        session.last_active = time.time()
        sid = session.session_id

        if sid not in self._progress:
            self._progress[sid] = {
                "msg_ts": None,
                "last_update": 0,
                "lines": [],
                "_full_text": "",
                "_bracket_hold": "",
            }

        state = self._progress[sid]

        if "_reactions" not in state and self._slack:
            state["_reactions"] = None

        if evt.raw_type == "assistant" and evt.text:
            if state["msg_ts"] is None:
                msg_ts = await self._slack.post_text(
                    session.channel_id, "\U0001f914 _thinking..._", session.thread_ts
                )
                state["msg_ts"] = msg_ts
                state["last_update"] = 0
                state["_start_time"] = time.time()
                await self._slack.set_thread_status(
                    session.channel_id, session.thread_ts, "is thinking..."
                )

            rc: StatusReactionController | None = state.get("_reactions")
            if rc:
                asyncio.ensure_future(rc.set_phase("thinking"))
                rc.on_progress()

            prev = state.get("_full_text", "")
            if prev and not evt.text.startswith(prev):
                new_text = prev + "\n\n" + evt.text
            else:
                new_text = evt.text
            state["_full_text"] = new_text

            # Filter out [OPTIONS: ...] from streaming display
            bracket_hold = state.get("_bracket_hold", "")
            filtered = ""
            for ch in new_text[len(new_text) - len(evt.text):]:
                if bracket_hold or ch == "[":
                    bracket_hold += ch
                    if ch == "]":
                        if bracket_hold.startswith("[OPTIONS:"):
                            bracket_hold = ""
                        else:
                            filtered += bracket_hold
                            bracket_hold = ""
                else:
                    filtered += ch
            state["_bracket_hold"] = bracket_hold

            now = time.time()
            if now - state["last_update"] >= _EDIT_INTERVAL and state["msg_ts"]:
                preview = new_text[-500:] if len(new_text) > 500 else new_text
                try:
                    await self._slack.web.chat_update(
                        channel=session.channel_id, ts=state["msg_ts"],
                        text=(preview + _CURSOR)[:_SLACK_MAX_TEXT]
                    )
                    state["last_update"] = now
                except Exception:
                    logger.debug("Failed to update streaming preview", exc_info=True)

        elif evt.raw_type == "assistant" and evt.tool_use:
            tool = evt.tool_use
            tool_name = tool.get("name", "")
            tool_input = tool.get("input", {})

            rc = state.get("_reactions")
            if rc:
                phase = tool_to_phase(tool_name)
                asyncio.ensure_future(rc.set_phase(phase))
                rc.on_progress()

            await self._slack.set_thread_status(
                session.channel_id, session.thread_ts, "is using " + tool_name
            )

            if tool_name == "Bash":
                detail = tool_input.get("command", "")[:60]
            elif tool_name in ("Read", "Write", "Edit"):
                detail = tool_input.get("file_path", "")[:60]
            else:
                detail = tool_name
            await self._update_progress(session, "\U0001fac6 `" + tool_name + "` " + detail, replace=True)

        elif evt.raw_type == "result":
            await self._slack.set_thread_status(session.channel_id, session.thread_ts, "")

            rc = state.get("_reactions")
            if rc:
                had_error = bool(evt.result.get("is_error"))
                asyncio.ensure_future(rc.finalize(error=had_error))

            final_text = state.get("_full_text", "")
            if final_text:
                await self._finalize_progress(session, final_text)
            elif sid in self._progress:
                self._progress.pop(sid, None)

            start_time = state.get("_start_time")
            if start_time:
                elapsed = time.time() - start_time
                if elapsed >= 60:
                    mins = int(elapsed // 60)
                    secs = int(elapsed % 60)
                    timing = "Finished in " + str(mins) + "m " + str(secs) + "s"
                else:
                    timing = "Finished in " + f"{elapsed:.1f}" + "s"
                await self._slack.post_text(
                    session.channel_id,
                    "_\u23f1 " + timing + "_",
                    session.thread_ts,
                )

            denials = evt.result.get("permission_denials", [])
            if denials:
                blocks = build_permission_denied_blocks(denials)
                await self._slack.post_blocks(
                    session.channel_id, blocks, "Permission denied", session.thread_ts
                )

    async def _on_process_exit(self, session_id: str, rc: int | None) -> None:
        from claude_slack_bridge.session_manager import SessionMode

        logger.info("Process exited for session %s (rc=%s)", session_id, rc)
        session = self._session_mgr.get(session_id)
        if not session:
            return

        state = self._progress.get(session_id, {})
        rc_ctrl: StatusReactionController | None = state.get("_reactions")
        if rc_ctrl:
            had_error = rc is not None and rc not in (0, 143)
            asyncio.ensure_future(rc_ctrl.finalize(error=had_error))

        if session.mode == SessionMode.PROCESS.value:
            self._session_mgr.set_mode(session_id, SessionMode.IDLE)
            self._progress.pop(session_id, None)
            if self._slack and rc and rc not in (0, 143):
                if rc == -9 or rc == 137:
                    msg = "\U0001f480 Claude process was killed (OOM or timeout). Please try again."
                elif rc == 1:
                    msg = "\u274c Claude process failed. Please try again."
                else:
                    msg = "\U0001f527 Claude process exited unexpectedly (code " + str(rc) + "). Please try again."
                await self._slack.post_text(
                    session.channel_id, msg, session.thread_ts,
                )
