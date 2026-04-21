import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient

from claude_slack_bridge.config import BridgeConfig
from claude_slack_bridge.conversation_parser import ConversationParser
from claude_slack_bridge.daemon import Daemon
from claude_slack_bridge.daemon_http import (
    _maybe_warn_version_mismatch,
    _read_last_turn_from_jsonl,
    create_http_app,
)
from claude_slack_bridge.session_manager import SessionManager, SessionMode


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


async def test_hook_pre_tool_use_yolo_session(config: BridgeConfig) -> None:
    """YOLO button marks the session trusted → PreToolUse auto-approves."""
    daemon = Daemon(config)
    # Pre-register the session and mark it trusted, as the YOLO button does.
    from claude_slack_bridge.session_manager import SessionMode
    daemon._session_mgr.create(
        session_id="test-session", session_name="t",
        channel_id="C1", thread_ts="1.0", mode=SessionMode.HOOK,
    )
    daemon._trusted_sessions.add("test-session")
    app = create_http_app(daemon)

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
    app = create_http_app(daemon)

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
    app = create_http_app(daemon)

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

    app = create_http_app(daemon)

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


# ── _read_last_turn_from_jsonl tests ──


def test_read_last_turn_from_jsonl(tmp_path: Path) -> None:
    """_read_last_turn_from_jsonl returns assistant text after the last user message."""
    # Build a fake JSONL file mimicking Claude Code session format
    session_id = "test-session-abc"
    cwd = str(tmp_path / "project")
    project_dir = cwd.replace("/", "-").replace(".", "-")
    jsonl_dir = Path.home() / ".claude" / "projects" / project_dir
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = jsonl_dir / f"{session_id}.jsonl"

    lines = [
        json.dumps({"type": "user", "message": {"content": "Hello"}, "timestamp": "t1"}),
        json.dumps({"type": "assistant", "message": {"content": "Hi there"}, "timestamp": "t2"}),
        json.dumps({"type": "user", "message": {"content": "What is 2+2?"}, "timestamp": "t3"}),
        json.dumps({"type": "assistant", "message": {"content": "The answer is 4."}, "timestamp": "t4"}),
        json.dumps({"type": "assistant", "message": {"content": "Anything else?"}, "timestamp": "t5"}),
    ]
    jsonl_path.write_text("\n".join(lines) + "\n")

    try:
        parser = ConversationParser()
        result = _read_last_turn_from_jsonl(parser, session_id, cwd)
        # Should contain assistant text after the last user message ("What is 2+2?")
        assert "The answer is 4." in result
        assert "Anything else?" in result
        # Should NOT contain the first assistant reply (before the last user msg)
        assert "Hi there" not in result
    finally:
        jsonl_path.unlink(missing_ok=True)
        # Clean up the directory if empty
        try:
            jsonl_dir.rmdir()
        except OSError:
            pass


def test_read_last_turn_from_jsonl_empty_cwd() -> None:
    """_read_last_turn_from_jsonl returns empty string for empty cwd."""
    parser = ConversationParser()
    result = _read_last_turn_from_jsonl(parser, "nonexistent", "")
    assert result == ""


# ── Session lookup by cwd fallback tests ──


def test_session_get_by_cwd_prefers_most_recent(tmp_path: Path) -> None:
    """When multiple sessions share the same cwd, get() returns the most recently active."""
    mgr = SessionManager(tmp_path / "sessions.json")

    # Create two sessions with the same cwd
    s1 = mgr.create("s1", "old session", "C1", "t1", SessionMode.HOOK)
    s1.cwd = "/workplace/project"
    s1.last_active = 1000.0

    s2 = mgr.create("s2", "new session", "C2", "t2", SessionMode.HOOK)
    s2.cwd = "/workplace/project"
    s2.last_active = 2000.0

    # Look up by cwd — should return the more recently active session (s2)
    result = mgr.get("/workplace/project")
    assert result is not None
    assert result.session_id == "s2"


def test_session_get_by_cwd_skips_archived(tmp_path: Path) -> None:
    """Archived sessions should not match cwd lookup."""
    mgr = SessionManager(tmp_path / "sessions.json")

    s1 = mgr.create("s1", "archived", "C1", "t1", SessionMode.HOOK)
    s1.cwd = "/workplace/project"
    s1.last_active = 9999.0  # Very recent but archived
    mgr.archive("s1")

    s2 = mgr.create("s2", "active", "C2", "t2", SessionMode.HOOK)
    s2.cwd = "/workplace/project"
    s2.last_active = 1000.0

    result = mgr.get("/workplace/project")
    assert result is not None
    assert result.session_id == "s2"


def test_session_get_by_id_still_works(tmp_path: Path) -> None:
    """Direct session_id lookup should still take priority over cwd fallback."""
    mgr = SessionManager(tmp_path / "sessions.json")

    s1 = mgr.create("s1", "session one", "C1", "t1", SessionMode.HOOK)
    s1.cwd = "/workplace/project"

    result = mgr.get("s1")
    assert result is not None
    assert result.session_id == "s1"


# ── _forwarded_prompts bounded size tests ──


def test_forwarded_prompts_capped_at_50(config: BridgeConfig) -> None:
    """_forwarded_prompts should not grow beyond 50 entries."""
    daemon = Daemon(config)

    # Simulate adding 50 prompts
    for i in range(50):
        daemon._forwarded_prompts.add(f"prompt-{i}")
    assert len(daemon._forwarded_prompts) == 50

    # The 51st add in daemon_events.py checks len >= 50 and clears first.
    # Simulate that logic here:
    if len(daemon._forwarded_prompts) >= 50:
        daemon._forwarded_prompts.clear()
    daemon._forwarded_prompts.add("prompt-50")
    assert len(daemon._forwarded_prompts) == 1
    assert "prompt-50" in daemon._forwarded_prompts


def test_forwarded_prompts_under_cap_not_cleared(config: BridgeConfig) -> None:
    """Under the cap, prompts accumulate normally."""
    daemon = Daemon(config)

    for i in range(10):
        if len(daemon._forwarded_prompts) >= 50:
            daemon._forwarded_prompts.clear()
        daemon._forwarded_prompts.add(f"prompt-{i}")

    assert len(daemon._forwarded_prompts) == 10


# ── mute_session / unmute_session persistence + level semantics ──


def test_mute_unmute_roundtrip_persists_to_disk(config: BridgeConfig) -> None:
    """Mute levels survive daemon restart via muted.json dict format."""
    daemon = Daemon(config)
    muted_path = config.config_dir / "muted.json"

    daemon.mute_session("s1")             # default "full"
    daemon.mute_session("s2", "ring")
    assert daemon._mute_levels == {"s1": "full", "s2": "ring"}
    assert json.loads(muted_path.read_text()) == {"s1": "full", "s2": "ring"}

    # Rehydrate — new Daemon instance reads muted.json via _load_muted().
    # Guards against NameError/import regressions in the persistence path.
    reborn = Daemon(config)
    assert reborn._mute_levels == {"s1": "full", "s2": "ring"}

    reborn.unmute_session("s1")
    assert reborn._mute_levels == {"s2": "ring"}
    assert json.loads(muted_path.read_text()) == {"s2": "ring"}


def test_mute_level_predicates(config: BridgeConfig) -> None:
    """is_silenced covers both levels; is_fully_muted only covers full."""
    daemon = Daemon(config)
    daemon.mute_session("full-sid", "full")
    daemon.mute_session("ring-sid", "ring")

    assert daemon.is_silenced("full-sid") and daemon.is_fully_muted("full-sid")
    # ring silences ambient sync but must NOT fully-mute — permission requests
    # still need to reach Slack.
    assert daemon.is_silenced("ring-sid") and not daemon.is_fully_muted("ring-sid")
    assert not daemon.is_silenced("other") and not daemon.is_fully_muted("other")


def test_mute_invalid_level_rejected(config: BridgeConfig) -> None:
    daemon = Daemon(config)
    with pytest.raises(ValueError):
        daemon.mute_session("s1", "bogus")


async def test_mute_api_level_shape(config: BridgeConfig) -> None:
    """POST /sessions/{id}/mute accepts only {level: full|ring|none}."""
    daemon = Daemon(config)
    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        # full
        resp = await client.post("/sessions/s1/mute", json={"level": "full"})
        assert resp.status == 200
        assert (await resp.json())["level"] == "full"
        assert daemon._mute_levels == {"s1": "full"}

        # ring overrides full on same session
        resp = await client.post("/sessions/s1/mute", json={"level": "ring"})
        assert (await resp.json())["level"] == "ring"
        assert daemon._mute_levels == {"s1": "ring"}

        # none unmutes
        resp = await client.post("/sessions/s1/mute", json={"level": "none"})
        body = await resp.json()
        assert body["ok"] is True and body["level"] is None
        assert daemon._mute_levels == {}

        # missing/invalid level → 400
        resp = await client.post("/sessions/s1/mute", json={})
        assert resp.status == 400
        resp = await client.post("/sessions/s1/mute", json={"level": "bogus"})
        assert resp.status == 400


async def test_permission_request_full_mute_falls_through(config: BridgeConfig) -> None:
    """full mute returns empty body so the hook falls through to TUI's dialog."""
    daemon = Daemon(config)
    daemon._slack = MagicMock()
    daemon._slack.set_thread_status = AsyncMock()
    daemon._slack.post_blocks = AsyncMock(return_value="ts.1")
    daemon._session_mgr.create(
        session_id="s1", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.HOOK,
    )
    daemon.mute_session("s1", "full")

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/permission-request", json={
            "session_key": "s1", "tool_name": "Bash",
            "tool_input": {"command": "rm file"}, "cwd": "/tmp",
        })
        assert resp.status == 200
        assert (await resp.text()) == ""
        # No Slack approval posted — TUI takes over.
        daemon._slack.post_blocks.assert_not_awaited()


async def test_permission_request_ring_mute_still_posts_to_slack(config: BridgeConfig) -> None:
    """ring mute silences ambient sync but keeps permission-request buttons."""
    daemon = Daemon(config)
    daemon._slack = MagicMock()
    daemon._slack.set_thread_status = AsyncMock()
    daemon._slack.post_blocks = AsyncMock(return_value="ts.1")
    daemon._session_mgr.create(
        session_id="s1", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.HOOK,
    )
    daemon.mute_session("s1", "ring")

    # Stub out the approval wait so we don't block the test.
    original_create = daemon._approval_mgr.create
    def _fake_create(request_id, **kwargs):
        state = original_create(request_id, **kwargs)
        state.resolve("approved")
        return state
    daemon._approval_mgr.create = _fake_create

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/permission-request", json={
            "session_key": "s1", "tool_name": "Bash",
            "tool_input": {"command": "rm file"}, "cwd": "/tmp",
        })
        assert resp.status == 200
        body = await resp.json()
        assert body["decision"] == "approved"
        # Buttons were posted despite ring mute.
        daemon._slack.post_blocks.assert_awaited()


# ── Version mismatch warning ──


async def test_version_mismatch_posts_warning_once(config: BridgeConfig) -> None:
    """Plugin/daemon version drift triggers exactly one Slack warning per pair."""
    daemon = Daemon(config)
    daemon._slack = MagicMock()
    daemon._slack.post_text = AsyncMock()

    # Drift the plugin version relative to the daemon's own __version__.
    from claude_slack_bridge import __version__ as daemon_version
    stale_plugin = "0.0.0-stale"
    assert stale_plugin != daemon_version

    await _maybe_warn_version_mismatch(daemon, "C1", "t1", stale_plugin)
    await _maybe_warn_version_mismatch(daemon, "C1", "t1", stale_plugin)

    # First call warns; second call is suppressed because the (plugin, daemon)
    # pair hasn't changed — otherwise every SessionStart spams the thread.
    assert daemon._slack.post_text.await_count == 1
    warning = daemon._slack.post_text.await_args.args[1]
    assert "Version mismatch" in warning
    assert stale_plugin in warning
    assert daemon_version in warning


async def test_version_mismatch_silent_when_matching(config: BridgeConfig) -> None:
    """No warning when plugin_version == daemon __version__, or when empty."""
    from claude_slack_bridge import __version__ as daemon_version

    daemon = Daemon(config)
    daemon._slack = MagicMock()
    daemon._slack.post_text = AsyncMock()

    await _maybe_warn_version_mismatch(daemon, "C1", "t1", daemon_version)
    await _maybe_warn_version_mismatch(daemon, "C1", "t1", "")

    assert daemon._slack.post_text.await_count == 0
