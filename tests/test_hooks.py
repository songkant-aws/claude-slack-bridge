import io
import json
import os
from unittest.mock import patch, MagicMock

import pytest

from claude_slack_bridge.hooks import (
    _build_hook_payload,
    _post_to_daemon,
    run_hook,
)


def test_build_hook_payload_pre_tool_use() -> None:
    stdin_json = {
        "hook_event_name": "preToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "cwd": "/workplace/proj",
        "session_id": "s1",
    }
    payload = _build_hook_payload("pre-tool-use", stdin_json)
    assert payload["event"] == "pre-tool-use"
    assert payload["tool_name"] == "Bash"
    assert payload["tool_input"] == {"command": "ls"}
    assert payload["session_key"] == "s1"
    assert payload["cwd"] == "/workplace/proj"


def test_build_hook_payload_session_key_fallback_cwd() -> None:
    stdin_json = {
        "hook_event_name": "preToolUse",
        "tool_name": "Read",
        "tool_input": {},
        "cwd": "/workplace/proj",
    }
    payload = _build_hook_payload("pre-tool-use", stdin_json)
    assert payload["session_key"] == "/workplace/proj"


def test_build_hook_payload_user_prompt() -> None:
    stdin_json = {
        "hook_event_name": "userPromptSubmit",
        "prompt": "Fix the bug",
        "cwd": "/workplace/proj",
        "session_id": "s1",
    }
    payload = _build_hook_payload("user-prompt", stdin_json)
    assert payload["event"] == "user-prompt"
    assert payload["prompt"] == "Fix the bug"


def test_build_hook_payload_stop() -> None:
    stdin_json = {
        "hook_event_name": "stop",
        "cwd": "/workplace/proj",
        "session_id": "s1",
        "stop_hook_active_tool_names": [],
    }
    payload = _build_hook_payload("stop", stdin_json)
    assert payload["event"] == "stop"


def test_post_to_daemon_connection_refused() -> None:
    """When daemon is down, returns 'error' instead of raising."""
    result = _post_to_daemon("/hooks/stop", {"event": "stop"}, port=19999, timeout=1)
    assert result == "error"


@patch.dict("os.environ", {}, clear=False)
@patch("claude_slack_bridge.hooks.sys")
@patch("claude_slack_bridge.hooks.load_config")
@patch("claude_slack_bridge.hooks._post_to_daemon")
def test_run_hook_pre_tool_use_approved(
    mock_post: MagicMock, mock_config: MagicMock, mock_sys: MagicMock
) -> None:
    # Ensure CLAUDE_SLACK_BRIDGE_PRINT is unset so the hook doesn't skip
    os.environ.pop("CLAUDE_SLACK_BRIDGE_PRINT", None)
    mock_sys.stdin.read.return_value = json.dumps({
        "hook_event_name": "preToolUse", "tool_name": "Bash",
        "tool_input": {"command": "ls"}, "cwd": "/proj", "session_id": "s1",
    })
    mock_config.return_value = MagicMock(approval_timeout_secs=300, daemon_port=7778)
    mock_post.return_value = "approved"
    assert run_hook("pre-tool-use") == 0


@patch.dict("os.environ", {}, clear=False)
@patch("claude_slack_bridge.hooks.sys")
@patch("claude_slack_bridge.hooks.load_config")
@patch("claude_slack_bridge.hooks._post_to_daemon")
def test_run_hook_pre_tool_use_rejected(
    mock_post: MagicMock, mock_config: MagicMock, mock_sys: MagicMock
) -> None:
    os.environ.pop("CLAUDE_SLACK_BRIDGE_PRINT", None)
    mock_sys.stdin.read.return_value = json.dumps({
        "hook_event_name": "preToolUse", "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /"}, "cwd": "/proj", "session_id": "s1",
    })
    mock_sys.stderr = io.StringIO()
    mock_config.return_value = MagicMock(approval_timeout_secs=300, daemon_port=7778)
    mock_post.return_value = "rejected"
    assert run_hook("pre-tool-use") == 2


@patch.dict("os.environ", {}, clear=False)
@patch("claude_slack_bridge.hooks.sys")
@patch("claude_slack_bridge.hooks.load_config")
@patch("claude_slack_bridge.hooks._post_to_daemon")
def test_run_hook_daemon_down_allows_tool(
    mock_post: MagicMock, mock_config: MagicMock, mock_sys: MagicMock
) -> None:
    os.environ.pop("CLAUDE_SLACK_BRIDGE_PRINT", None)
    mock_sys.stdin.read.return_value = json.dumps({
        "hook_event_name": "preToolUse", "tool_name": "Bash",
        "tool_input": {}, "cwd": "/proj",
    })
    mock_config.return_value = MagicMock(approval_timeout_secs=300, daemon_port=7778)
    mock_post.return_value = "error"
    assert run_hook("pre-tool-use") == 0  # daemon down = allow
