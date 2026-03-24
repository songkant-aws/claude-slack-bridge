import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from claude_slack_bridge.approval import ApprovalManager
from claude_slack_bridge.config import BridgeConfig
from claude_slack_bridge.http_api import create_app
from claude_slack_bridge.registry import SessionRegistry


@pytest.fixture
def config() -> BridgeConfig:
    return BridgeConfig(require_approval=True, default_channel="C12345")


@pytest.fixture
def registry(tmp_config_dir) -> SessionRegistry:
    return SessionRegistry(storage_path=tmp_config_dir / "sessions.json")


@pytest.fixture
def approval_mgr() -> ApprovalManager:
    return ApprovalManager()


@pytest.fixture
def slack_mock() -> AsyncMock:
    mock = AsyncMock()
    mock.post_blocks = AsyncMock(return_value="1234.5678")
    mock.update_blocks = AsyncMock()
    mock.post_text = AsyncMock(return_value="1234.5678")
    return mock


@pytest.fixture
def app(config, registry, approval_mgr, slack_mock) -> web.Application:
    return create_app(config, registry, approval_mgr, slack_mock)


@pytest.fixture
async def client(aiohttp_client, app) -> TestClient:
    return await aiohttp_client(app)


async def test_health(client: TestClient) -> None:
    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"


async def test_list_sessions_empty(client: TestClient) -> None:
    resp = await client.get("/sessions")
    assert resp.status == 200
    data = await resp.json()
    assert data == []


async def test_post_tool_use_auto_registers_session(
    client: TestClient,
    registry: SessionRegistry,
    slack_mock: AsyncMock,
) -> None:
    payload = {
        "event": "post-tool-use",
        "session_key": "sess-1",
        "cwd": "/workplace/proj",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/x.py"},
        "tool_output": "file contents",
        "duration_ms": 100,
    }
    resp = await client.post(
        "/hooks/post-tool-use",
        json=payload,
    )
    assert resp.status == 200
    # Session should be auto-registered
    assert registry.get("sess-1") is not None
    # Slack message should be posted
    assert slack_mock.post_blocks.called


async def test_user_prompt_hook(
    client: TestClient,
    slack_mock: AsyncMock,
) -> None:
    payload = {
        "event": "user-prompt",
        "session_key": "sess-1",
        "cwd": "/workplace/proj",
        "prompt": "Fix the bug",
    }
    resp = await client.post("/hooks/user-prompt", json=payload)
    assert resp.status == 200


async def test_stop_hook(
    client: TestClient,
    slack_mock: AsyncMock,
) -> None:
    payload = {
        "event": "stop",
        "session_key": "sess-1",
        "cwd": "/workplace/proj",
        "response": "Done!",
    }
    resp = await client.post("/hooks/stop", json=payload)
    assert resp.status == 200


async def test_pre_tool_use_approval_flow(
    client: TestClient,
    approval_mgr: ApprovalManager,
    slack_mock: AsyncMock,
) -> None:
    """Test the blocking pre-tool-use approval with async resolution."""
    payload = {
        "event": "pre-tool-use",
        "session_key": "sess-approve",
        "cwd": "/workplace/proj",
        "tool_name": "Bash",
        "tool_input": {"command": "npm install"},
        "request_id": "test-req-1",
    }

    async def _approve_after_delay() -> None:
        await asyncio.sleep(0.1)
        approval_mgr.resolve("test-req-1", "approved")

    task = asyncio.create_task(_approve_after_delay())
    resp = await client.post("/hooks/pre-tool-use", json=payload)
    assert resp.status == 200
    text = await resp.text()
    assert text == "approved"
    await task
    # Verify approval buttons were posted then updated
    assert slack_mock.post_blocks.called
    assert slack_mock.update_blocks.called
