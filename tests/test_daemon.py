import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient

from claude_slack_bridge.config import BridgeConfig
from claude_slack_bridge.daemon import Daemon
from claude_slack_bridge.session_manager import SessionMode


@pytest.fixture
def config(tmp_config_dir: Path) -> BridgeConfig:
    return BridgeConfig(
        config_dir=tmp_config_dir,
        slack_app_token="xapp-test",
        slack_bot_token="xoxb-test",
    )


def test_daemon_init(config: BridgeConfig) -> None:
    daemon = Daemon(config)
    assert daemon._config is config
    assert daemon._session_mgr is not None
    assert daemon._approval_mgr is not None
    assert daemon._pool is not None



async def test_daemon_handle_interactive_approve(config: BridgeConfig) -> None:
    daemon = Daemon(config)
    state = daemon._approval_mgr.create("req-123")

    await daemon._handle_interactive(
        action={"action_id": "approve_tool", "value": "req-123"},
        payload={},
    )

    assert state.status == "approved"


async def test_daemon_handle_interactive_reject(config: BridgeConfig) -> None:
    daemon = Daemon(config)
    state = daemon._approval_mgr.create("req-123")

    await daemon._handle_interactive(
        action={"action_id": "reject_tool", "value": "req-123"},
        payload={},
    )

    assert state.status == "rejected"


async def test_hook_pre_tool_use_yolo_mode(config: BridgeConfig) -> None:
    """Issue #2: PreToolUse should auto-approve in YOLO mode."""
    daemon = Daemon(config)
    daemon._yolo_mode = True
    app = daemon._create_http_app()

    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/pre-tool-use",
            json={
                "session_key": "test-session",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "cwd": "/tmp",
            },
        )
        assert resp.status == 200
        text = await resp.text()
        assert text == "approved"


async def test_hook_pre_tool_use_auto_approve_safe_tool(config: BridgeConfig) -> None:
    """Issue #2: Safe tools (Read, Glob, Grep) should auto-approve."""
    daemon = Daemon(config)
    app = daemon._create_http_app()

    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        for tool in ["Read", "Glob", "Grep"]:
            resp = await client.post(
                "/hooks/pre-tool-use",
                json={
                    "session_key": "test-session",
                    "tool_name": tool,
                    "tool_input": {},
                    "cwd": "/tmp",
                },
            )
            assert resp.status == 200
            text = await resp.text()
            assert text == "approved", f"{tool} should be auto-approved"


async def test_hook_pre_tool_use_no_slack_approves(config: BridgeConfig) -> None:
    """Issue #2: Without Slack connection, fail open (approve)."""
    daemon = Daemon(config)
    daemon._slack = None  # No Slack
    app = daemon._create_http_app()

    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/pre-tool-use",
            json={
                "session_key": "unknown-session",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
                "cwd": "/tmp",
            },
        )
        assert resp.status == 200
        text = await resp.text()
        assert text == "approved"


async def test_hook_pre_tool_use_trusted_session(config: BridgeConfig) -> None:
    """Issue #2: Trusted sessions should auto-approve."""
    daemon = Daemon(config)

    # Create a session and trust it
    session = daemon._session_mgr.create(
        session_id="s1", session_name="test",
        channel_id="C123", thread_ts="t1",
        mode=SessionMode.HOOK,
    )
    daemon._trusted_sessions.add("s1")

    app = daemon._create_http_app()

    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/hooks/pre-tool-use",
            json={
                "session_key": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
                "cwd": "/tmp",
            },
        )
        assert resp.status == 200
        text = await resp.text()
        assert text == "approved"
