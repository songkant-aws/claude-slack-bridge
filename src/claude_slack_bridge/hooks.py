from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error

from claude_slack_bridge.config import BridgeConfig, load_config


def _build_hook_payload(event_type: str, stdin_json: dict) -> dict:
    """Build a normalized payload from hook stdin JSON."""
    session_key = BridgeConfig.derive_session_key(stdin_json)
    payload: dict = {
        "event": event_type,
        "session_key": session_key,
        "cwd": stdin_json.get("cwd", ""),
    }

    if event_type in ("pre-tool-use", "post-tool-use"):
        payload["tool_name"] = stdin_json.get("tool_name", "")
        payload["tool_input"] = stdin_json.get("tool_input", {})
        payload["tool_use_id"] = stdin_json.get("tool_use_id", "")
    if event_type == "post-tool-use":
        payload["tool_output"] = stdin_json.get("tool_output", "")
        payload["duration_ms"] = stdin_json.get("duration_ms", 0)
    if event_type == "user-prompt":
        payload["prompt"] = stdin_json.get("prompt", "")
    if event_type == "stop":
        payload["response"] = stdin_json.get("last_assistant_message", "") or stdin_json.get("response", "")
    if event_type == "notification":
        payload["notification_type"] = stdin_json.get("notification_type", "")
        payload["message"] = stdin_json.get("message", "")
    if event_type == "pre-compact":
        payload["compact_type"] = stdin_json.get("compact_type", "auto")

    return payload


def _post_to_daemon(endpoint: str, payload: dict, port: int, timeout: int) -> str:
    """POST JSON to daemon. Returns response body or 'error'."""
    url = f"http://127.0.0.1:{port}{endpoint}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode().strip()
    except (urllib.error.URLError, OSError):
        # Daemon unreachable — exit 0 to not block Claude Code
        return "error"


def run_hook(event_type: str) -> int:
    """Main entry point for hook subcommands. Returns exit code."""
    # Skip hooks from our own --print process
    if os.environ.get("CLAUDE_SLACK_BRIDGE_PRINT"):
        return 0

    stdin_data = sys.stdin.read()
    try:
        stdin_json = json.loads(stdin_data) if stdin_data.strip() else {}
    except json.JSONDecodeError:
        return 0  # Don't block Claude Code on bad input

    cfg = load_config()
    payload = _build_hook_payload(event_type, stdin_json)

    endpoint = f"/hooks/{event_type}"

    if event_type == "pre-tool-use":
        # Blocking call — wait for approval
        timeout = cfg.approval_timeout_secs + 10
        result = _post_to_daemon(endpoint, payload, cfg.daemon_port, timeout)
        if result == "approved" or result == "error":
            return 0  # approve or daemon down
        else:
            sys.stderr.write(f"Tool rejected via Slack approval\n")
            return 2
    else:
        # Non-blocking — fire and forget
        _post_to_daemon(endpoint, payload, cfg.daemon_port, timeout=5)
        return 0
