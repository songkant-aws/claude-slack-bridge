"""Parse Claude Code --output-format stream-json events."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class StreamEvent:
    """Parsed stream-json event."""
    raw_type: str          # "system", "assistant", "result"
    subtype: str = ""      # "init", "hook_started", "success", etc.
    session_id: str = ""
    text: str = ""         # Accumulated assistant text
    tool_use: dict = field(default_factory=dict)  # {name, input, id}
    result: dict = field(default_factory=dict)     # Final result payload
    raw: dict = field(default_factory=dict)


def parse_line(line: str) -> StreamEvent | None:
    """Parse a single line of stream-json output. Returns None for unparseable lines."""
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.debug("Unparseable stream line: %s", line[:200])
        return None

    evt_type = data.get("type", "")
    evt = StreamEvent(raw_type=evt_type, raw=data)
    evt.subtype = data.get("subtype", "")
    evt.session_id = data.get("session_id", "")

    if evt_type == "assistant":
        msg = data.get("message", {})
        content = msg.get("content", [])
        for block in content:
            if block.get("type") == "text":
                evt.text += block.get("text", "")
            elif block.get("type") == "tool_use":
                evt.tool_use = {
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input": block.get("input", {}),
                }

    elif evt_type == "result":
        evt.text = data.get("result", "")
        evt.result = data

    return evt
