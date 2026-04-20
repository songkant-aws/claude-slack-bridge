from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


_DEFAULT_CONFIG_DIR = Path.home() / ".claude" / "slack-bridge"


@dataclass
class BridgeConfig:
    config_dir: Path = _DEFAULT_CONFIG_DIR
    daemon_port: int = 7778
    approval_timeout_secs: int = 300
    auto_approve_tools: list[str] = field(default_factory=lambda: ["Read", "Glob", "Grep"])
    session_archive_after_secs: int = 86400
    log_level: str = "INFO"
    max_concurrent_sessions: int = 5
    process_idle_timeout_secs: int = 600  # kill idle --print process after 10min
    claude_args: list[str] = field(default_factory=list)
    work_dir: str = ""  # cwd for claude processes; empty = home dir
    slack_app_token: str = ""
    slack_bot_token: str = ""

    @staticmethod
    def derive_session_key(hook_json: dict) -> str:
        """Derive a session key from hook stdin JSON.

        Strategy:
        1. Use session_id if present.
        2. Hash cwd + CLAUDE_CODE_SESSION_KEY env var if set.
        3. Fall back to cwd alone.
        """
        if sid := hook_json.get("session_id"):
            return str(sid)

        cwd = hook_json.get("cwd", "unknown")
        env_key = os.environ.get("CLAUDE_CODE_SESSION_KEY", "")
        if env_key:
            raw = f"{cwd}:{env_key}"
            return hashlib.sha256(raw.encode()).hexdigest()[:16]

        return cwd


def load_config(config_dir: Path | None = None) -> BridgeConfig:
    """Load bridge config from config.json and .env files."""
    cdir = config_dir or _DEFAULT_CONFIG_DIR

    # Load config.json
    config_file = cdir / "config.json"
    file_data: dict = {}
    if config_file.is_file():
        with open(config_file) as f:
            file_data = json.load(f)

    # Load .env for tokens
    app_token = ""
    bot_token = ""
    env_file = cdir / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k == "SLACK_APP_TOKEN":
                    app_token = v
                elif k == "SLACK_BOT_TOKEN":
                    bot_token = v

    return BridgeConfig(
        config_dir=cdir,
        daemon_port=file_data.get("daemon_port", 7778),
        approval_timeout_secs=file_data.get("approval_timeout_secs", 300),
        auto_approve_tools=file_data.get("auto_approve_tools", ["Read", "Glob", "Grep"]),
        session_archive_after_secs=file_data.get("session_archive_after_secs", 86400),
        log_level=file_data.get("log_level", "INFO"),
        max_concurrent_sessions=file_data.get("max_concurrent_sessions", 5),
        process_idle_timeout_secs=file_data.get("process_idle_timeout_secs", 600),
        claude_args=file_data.get("claude_args", []),
        work_dir=file_data.get("work_dir", str(Path.home())),
        slack_app_token=app_token,
        slack_bot_token=bot_token,
    )
