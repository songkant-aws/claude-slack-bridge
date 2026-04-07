"""Incremental JSONL conversation parser for Claude Code session files.

Watches ~/.claude/projects/{project}/{session}.jsonl and extracts
conversation messages incrementally (only reads new bytes since last parse).
"""
from __future__ import annotations

import json
import logging
import os
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("claude_slack_bridge")


@dataclass
class ChatMessage:
    role: str  # "user", "assistant", "tool_use", "tool_result"
    text: str = ""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_use_id: str = ""
    is_error: bool = False
    timestamp: str = ""


@dataclass
class _ParseState:
    last_offset: int = 0
    messages: list[ChatMessage] = field(default_factory=list)
    seen_tool_ids: set[str] = field(default_factory=set)


def _session_file_path(session_id: str, cwd: str) -> str:
    project_dir = cwd.replace("/", "-").replace(".", "-")
    return os.path.expanduser(f"~/.claude/projects/{project_dir}/{session_id}.jsonl")


class ConversationParser:
    """Incremental parser for Claude Code JSONL session files."""

    def __init__(self) -> None:
        self._states: dict[str, _ParseState] = {}

    def parse_incremental(self, session_id: str, cwd: str) -> list[ChatMessage]:
        """Parse only new lines since last call. Returns new messages."""
        path = _session_file_path(session_id, cwd)
        if not os.path.exists(path):
            return []

        state = self._states.setdefault(session_id, _ParseState())

        try:
            file_size = os.path.getsize(path)
        except OSError:
            return []

        if file_size <= state.last_offset:
            return []

        new_messages: list[ChatMessage] = []
        try:
            with open(path, "r") as f:
                f.seek(state.last_offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    msg = self._parse_line(line, state)
                    if msg:
                        new_messages.append(msg)
                        state.messages.append(msg)
                state.last_offset = f.tell()
        except (OSError, UnicodeDecodeError):
            pass

        return new_messages

    def get_all_messages(self, session_id: str) -> list[ChatMessage]:
        state = self._states.get(session_id)
        return state.messages if state else []

    def reset(self, session_id: str) -> None:
        self._states.pop(session_id, None)

    def _parse_line(self, line: str, state: _ParseState) -> Optional[ChatMessage]:
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            return None

        msg_type = j.get("type")
        if j.get("isMeta"):
            return None

        message = j.get("message", {})
        ts = j.get("timestamp", "")

        if msg_type == "user":
            content = message.get("content", "")
            if isinstance(content, str):
                if content.startswith(("<command-name>", "<local-command", "Caveat:")):
                    return None
                return ChatMessage(role="user", text=content, timestamp=ts)

        elif msg_type == "assistant":
            content = message.get("content")
            if isinstance(content, str):
                return ChatMessage(role="assistant", text=content, timestamp=ts)
            elif isinstance(content, list):
                # Extract text blocks and tool_use blocks
                for block in content:
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text", "")
                        if text and not text.startswith("[Request interrupted"):
                            return ChatMessage(role="assistant", text=text, timestamp=ts)
                    elif btype == "tool_use":
                        tid = block.get("id", "")
                        if tid in state.seen_tool_ids:
                            continue
                        state.seen_tool_ids.add(tid)
                        return ChatMessage(
                            role="tool_use",
                            tool_name=block.get("name", ""),
                            tool_input=block.get("input", {}),
                            tool_use_id=tid,
                            timestamp=ts,
                        )

        return None


class SessionFileWatcher:
    """Watches JSONL session files for changes using polling.

    On Linux (cloud desktop), inotify would be ideal but polling at 500ms
    is simple and sufficient for Slack sync latency.
    """

    def __init__(self, parser: ConversationParser, on_new_messages=None) -> None:
        self._parser = parser
        self._on_new_messages = on_new_messages
        self._watching: dict[str, str] = {}  # session_id -> cwd
        self._task: Optional[asyncio.Task] = None

    def watch(self, session_id: str, cwd: str) -> None:
        self._watching[session_id] = cwd
        if not self._task or self._task.done():
            self._task = asyncio.ensure_future(self._poll_loop())

    def unwatch(self, session_id: str) -> None:
        self._watching.pop(session_id, None)

    async def _poll_loop(self) -> None:
        while self._watching:
            for sid, cwd in list(self._watching.items()):
                new_msgs = self._parser.parse_incremental(sid, cwd)
                if new_msgs and self._on_new_messages:
                    await self._on_new_messages(sid, new_msgs)
            await asyncio.sleep(0.5)

    def stop(self) -> None:
        self._watching.clear()
        if self._task:
            self._task.cancel()
