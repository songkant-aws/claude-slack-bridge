from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import pytest

from claude_slack_bridge.config import BridgeConfig
from claude_slack_bridge.daemon import Daemon


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
