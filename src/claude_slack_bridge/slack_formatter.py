from __future__ import annotations

import json
import re

_OPTIONS_RE = re.compile(r"\[OPTIONS:\s*(.+?)\]\s*$", re.MULTILINE)
OPTIONS_ACTION_PREFIX = "options_choice_"

# Markdown → Slack mrkdwn conversion patterns
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_HEADER = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_CODE_LANG = re.compile(r"```\w*\n", re.MULTILINE)
_MD_IMG = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def md_to_mrkdwn(text: str) -> str:
    """Convert Markdown to Slack mrkdwn format.

    Preserves content inside code blocks (``` ... ```) from being transformed.
    """
    # Split on code fences, preserving them
    parts = re.split(r"(```[\s\S]*?```)", text)
    result = []
    for i, part in enumerate(parts):
        if part.startswith("```"):
            # Inside code block — only strip language identifier
            result.append(_MD_CODE_LANG.sub("```\n", part))
        else:
            # Outside code block — apply all conversions
            part = _MD_IMG.sub(r"<\2|\1>", part)       # images → links
            part = _MD_LINK.sub(r"<\2|\1>", part)      # [text](url) → <url|text>
            part = _MD_BOLD.sub(r"*\1*", part)         # **bold** → *bold*
            part = _MD_HEADER.sub(r"*\1*", part)       # # Header → *Header*
            result.append(part)
    return "".join(result)


def extract_options(text: str) -> tuple[str, list[str]]:
    """Extract [OPTIONS: A | B | C] from text. Returns (cleaned_text, choices)."""
    m = _OPTIONS_RE.search(text)
    if not m:
        return text, []
    choices = [c.strip() for c in m.group(1).split("|") if c.strip()]
    return text[: m.start()].rstrip(), choices


def build_options_blocks(choices: list[str]) -> list[dict]:
    """Build Slack action buttons from OPTIONS choices."""
    buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": c[:75]},
            "action_id": f"{OPTIONS_ACTION_PREFIX}{i}",
            "value": c,
        }
        for i, c in enumerate(choices[:5])
    ]
    return [{"type": "actions", "elements": buttons}]


def truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 30] + "\n... (truncated, full output too long)"


def _code_block(text: str, max_chars: int = 2500) -> str:
    truncated = truncate_text(text, max_chars)
    return f"```\n{truncated}\n```"


def build_session_header_blocks(
    session_id: str,
    directory: str,
) -> list[dict]:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    resume_cmd = f"cd {directory} && claude --resume {session_id}" if directory else f"claude --resume {session_id}"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*New Claude Code Session*\n"
                    f"Started: {now}\n\n"
                    f"Resume in TUI:\n"
                    f"`{resume_cmd}`"
                ),
            },
        },
        {"type": "divider"},
    ]


def build_approval_blocks(
    tool_name: str,
    tool_input: dict,
    session_id: str,
    session_name: str,
    request_id: str,
) -> list[dict]:
    if tool_name == "Bash":
        detail = tool_input.get("command", json.dumps(tool_input))
    elif tool_name in ("Read", "Write", "Edit"):
        detail = tool_input.get("file_path", json.dumps(tool_input))
    else:
        detail = json.dumps(tool_input, indent=2)

    detail = truncate_text(str(detail), 2500)

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🔐 *Tool Request: {tool_name}*\n"
                    f"Session: `{session_name}`\n\n"
                    f"```\n{detail}\n```"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve"},
                    "style": "primary",
                    "action_id": "approve_tool",
                    "value": request_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔓 Trust Session"},
                    "action_id": "trust_session",
                    "value": session_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⚡ YOLO"},
                    "action_id": "yolo_mode",
                    "value": request_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Reject"},
                    "style": "danger",
                    "action_id": "reject_tool",
                    "value": request_id,
                },
            ],
        },
    ]


def build_tool_notification_blocks(
    tool_name: str,
    tool_input: dict,
) -> list[dict]:
    """Notification-only block for PROCESS mode (no approval buttons)."""
    if tool_name == "Bash":
        detail = tool_input.get("command", json.dumps(tool_input))
    elif tool_name in ("Read", "Write", "Edit"):
        detail = tool_input.get("file_path", json.dumps(tool_input))
    else:
        detail = json.dumps(tool_input, indent=2)

    detail = truncate_text(str(detail), 2500)

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🔧 *{tool_name}*\n```\n{detail}\n```",
            },
        },
    ]


def build_permission_denied_blocks(denials: list[dict]) -> list[dict]:
    """Show permission denials from Claude Code."""
    lines = []
    for d in denials[:5]:
        tool = d.get("tool_name", "unknown")
        reason = d.get("reason", "permission denied")
        lines.append(f"• *{tool}*: {reason}")
    text = "🚫 *Permission Denied*\n" + "\n".join(lines)
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def build_post_tool_blocks(
    tool_name: str,
    tool_input: dict,
    output: str,
    duration_ms: float = 0,
) -> list[dict]:
    duration_str = f" ({duration_ms / 1000:.1f}s)" if duration_ms else ""

    if tool_name == "Bash":
        summary = tool_input.get("command", tool_name)
    elif tool_name in ("Read", "Write", "Edit", "Glob", "Grep"):
        summary = tool_input.get("file_path", tool_input.get("pattern", tool_name))
    else:
        summary = tool_name

    output_text = truncate_text(output, 2500) if output else "(no output)"

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{tool_name}*{duration_str}\n"
                    f"> {truncate_text(str(summary), 200)}\n"
                    f"```\n{output_text}\n```"
                ),
            },
        },
    ]


def build_response_blocks(response_text: str) -> list[dict]:
    text = md_to_mrkdwn(truncate_text(response_text, 2900))
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Claude Response*\n\n{text}",
            },
        },
    ]


def build_user_prompt_blocks(prompt: str) -> list[dict]:
    text = truncate_text(prompt, 2900)
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*User Prompt*\n\n{text}",
            },
        },
    ]


def build_approval_resolved_blocks(
    tool_name: str,
    decision: str,
    request_id: str,
) -> list[dict]:
    emoji = "white_check_mark" if decision == "approved" else "x"
    label = "Approved" if decision == "approved" else "Rejected"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":{emoji}: *{tool_name}* — {label}",
            },
        },
    ]
