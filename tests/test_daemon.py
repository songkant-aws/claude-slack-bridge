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
    assert daemon._registry is not None
    assert daemon._approval_mgr is not None


@patch("claude_slack_bridge.daemon.SlackClient")
async def test_daemon_handle_interactive_approve(
    mock_slack_cls: MagicMock,
    config: BridgeConfig,
) -> None:
    daemon = Daemon(config)
    state = daemon._approval_mgr.create("req-123")

    await daemon._handle_interactive_action(
        action_id="approve_tool",
        value="req-123",
        channel="C123",
        msg_ts="1234.5678",
    )

    assert state.status == "approved"


@patch("claude_slack_bridge.daemon.SlackClient")
async def test_daemon_handle_interactive_reject(
    mock_slack_cls: MagicMock,
    config: BridgeConfig,
) -> None:
    daemon = Daemon(config)
    state = daemon._approval_mgr.create("req-123")

    await daemon._handle_interactive_action(
        action_id="reject_tool",
        value="req-123",
        channel="C123",
        msg_ts="1234.5678",
    )

    assert state.status == "rejected"
