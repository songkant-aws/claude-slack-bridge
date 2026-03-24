import json
from pathlib import Path

import pytest

from claude_slack_bridge.config import BridgeConfig, load_config


def test_load_default_config_when_no_file(tmp_config_dir: Path) -> None:
    cfg = load_config(config_dir=tmp_config_dir)
    assert cfg.daemon_port == 7778
    assert cfg.default_channel == "claude-code"
    assert cfg.approval_timeout_secs == 300
    assert cfg.auto_approve_tools == ["Read", "Glob", "Grep"]
    assert cfg.require_approval is True
    assert cfg.truncate_chars == 3000
    assert cfg.log_level == "INFO"


def test_load_config_from_file(tmp_config_dir: Path) -> None:
    config_file = tmp_config_dir / "config.json"
    config_file.write_text(json.dumps({
        "daemon_port": 9999,
        "default_channel": "my-channel",
        "require_approval": False,
    }))
    cfg = load_config(config_dir=tmp_config_dir)
    assert cfg.daemon_port == 9999
    assert cfg.default_channel == "my-channel"
    assert cfg.require_approval is False
    # Defaults still apply for unset fields
    assert cfg.approval_timeout_secs == 300


def test_load_env_tokens(tmp_config_dir: Path) -> None:
    env_file = tmp_config_dir / ".env"
    env_file.write_text("SLACK_APP_TOKEN=xapp-test\nSLACK_BOT_TOKEN=xoxb-test\n")
    cfg = load_config(config_dir=tmp_config_dir)
    assert cfg.slack_app_token == "xapp-test"
    assert cfg.slack_bot_token == "xoxb-test"


def test_load_env_missing_tokens(tmp_config_dir: Path) -> None:
    cfg = load_config(config_dir=tmp_config_dir)
    assert cfg.slack_app_token == ""
    assert cfg.slack_bot_token == ""


def test_config_dir_default() -> None:
    cfg = load_config()
    expected = Path.home() / ".claude" / "slack-bridge"
    assert cfg.config_dir == expected


def test_session_key_from_hook_json_with_session_id() -> None:
    hook_json = {"session_id": "abc123", "cwd": "/some/path"}
    assert BridgeConfig.derive_session_key(hook_json) == "abc123"


def test_session_key_fallback_to_cwd() -> None:
    hook_json = {"cwd": "/workplace/qianheng/my-project"}
    key = BridgeConfig.derive_session_key(hook_json)
    assert key == "/workplace/qianheng/my-project"


def test_session_key_with_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_KEY", "custom-key")
    hook_json = {"cwd": "/some/path"}
    key = BridgeConfig.derive_session_key(hook_json)
    assert len(key) == 16  # 16-char hex hash
    assert key != "/some/path"  # should be a hash, not raw cwd
