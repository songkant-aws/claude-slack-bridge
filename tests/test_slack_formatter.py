import json

from claude_slack_bridge.slack_formatter import (
    build_approval_blocks,
    build_post_tool_blocks,
    build_response_blocks,
    build_session_header_blocks,
    build_user_prompt_blocks,
    truncate_text,
)


def test_build_session_header_blocks() -> None:
    blocks = build_session_header_blocks(
        session_id="abc123",
        directory="/workplace/my-project",
    )
    text = json.dumps(blocks)
    assert "claude --resume abc123" in text
    assert any(b["type"] == "section" for b in blocks)


def test_build_approval_blocks_with_bash() -> None:
    blocks = build_approval_blocks(
        tool_name="Bash",
        tool_input={"command": "npm install"},
        session_id="abc123",
        session_name="/workplace/my-project",
        request_id="req-1",
    )
    text = json.dumps(blocks)
    assert "Bash" in text
    assert "npm install" in text
    # Must have actions block with approve/reject buttons
    actions = [b for b in blocks if b["type"] == "actions"]
    assert len(actions) == 1
    buttons = actions[0]["elements"]
    assert len(buttons) == 4
    assert buttons[0]["action_id"] == "approve_tool"
    assert buttons[1]["action_id"] == "trust_session"
    assert buttons[2]["action_id"] == "yolo_mode"
    assert buttons[3]["action_id"] == "reject_tool"
    assert buttons[0]["value"] == "req-1"


def test_build_post_tool_blocks() -> None:
    blocks = build_post_tool_blocks(
        tool_name="Bash",
        tool_input={"command": "echo hello"},
        output="hello\n",
        duration_ms=1500,
    )
    text = json.dumps(blocks)
    assert "Bash" in text
    assert "hello" in text
    assert "1.5s" in text


def test_build_response_blocks() -> None:
    blocks = build_response_blocks(response_text="Build succeeded.")
    text = json.dumps(blocks)
    assert "Build succeeded." in text


def test_build_user_prompt_blocks() -> None:
    blocks = build_user_prompt_blocks(prompt="Fix the login bug")
    text = json.dumps(blocks)
    assert "Fix the login bug" in text


def test_truncate_text_short() -> None:
    assert truncate_text("hello", 3000) == "hello"


def test_truncate_text_long() -> None:
    long = "x" * 5000
    result = truncate_text(long, 3000)
    assert len(result) <= 3000 + 50  # allow for truncation suffix
    assert "truncated" in result.lower()


def test_approval_blocks_truncate_large_input() -> None:
    blocks = build_approval_blocks(
        tool_name="Write",
        tool_input={"content": "x" * 5000, "file_path": "/tmp/test.py"},
        session_id="s1",
        session_name="proj",
        request_id="r1",
    )
    text = json.dumps(blocks)
    # Should not contain the full 5000 chars
    assert len(text) < 5000
