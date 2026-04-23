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
    _strip_wrapper_blocks,
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


def test_read_last_turn_prefixes_blocks_with_bullet(tmp_path: Path) -> None:
    """Each assistant block gets a leading ●, matching the streaming progress
    message so Slack's finalized text keeps the same visual rhythm as the TUI.
    """
    session_id = "test-bullets-xyz"
    cwd = str(tmp_path / "bullets")
    project_dir = cwd.replace("/", "-").replace(".", "-")
    jsonl_dir = Path.home() / ".claude" / "projects" / project_dir
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = jsonl_dir / f"{session_id}.jsonl"

    lines = [
        json.dumps({"type": "user", "message": {"content": "go"}, "timestamp": "t1"}),
        json.dumps({"type": "assistant", "message": {"content": "first thought"}, "timestamp": "t2"}),
        json.dumps({"type": "assistant", "message": {"content": "second thought"}, "timestamp": "t3"}),
    ]
    jsonl_path.write_text("\n".join(lines) + "\n")

    try:
        parser = ConversationParser()
        result = _read_last_turn_from_jsonl(parser, session_id, cwd)
        # Both blocks present, each with the bullet prefix.
        assert result.startswith("● first thought")
        assert "● second thought" in result
        # Exactly two bullets (one per assistant block), not one global prefix.
        assert result.count("● ") == 2
    finally:
        jsonl_path.unlink(missing_ok=True)
        try:
            jsonl_dir.rmdir()
        except OSError:
            pass


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


# ── Mute level persistence + semantics ──


def test_mute_levels_roundtrip_persists_to_disk(config: BridgeConfig) -> None:
    """set_mute_level/clear_mute_level survive daemon restart via muted.json."""
    daemon = Daemon(config)
    muted_path = config.config_dir / "muted.json"

    daemon.set_mute_level("s1", "sync")
    daemon.set_mute_level("s2", "ring")
    assert daemon._mute_levels == {"s1": "sync", "s2": "ring"}
    assert json.loads(muted_path.read_text()) == {"s1": "sync", "s2": "ring"}

    # Rehydrate — new Daemon instance reads muted.json via _load_muted().
    # Guards against NameError/import regressions in the persistence path.
    reborn = Daemon(config)
    assert reborn._mute_levels == {"s1": "sync", "s2": "ring"}

    reborn.clear_mute_level("s1")
    assert reborn._mute_levels == {"s2": "ring"}
    assert json.loads(muted_path.read_text()) == {"s2": "ring"}


def test_default_mute_semantics(config: BridgeConfig) -> None:
    """Unknown sessions are fully muted by default — new CC sessions stay quiet."""
    daemon = Daemon(config)
    assert daemon.is_silenced("never-seen") is True
    assert daemon.is_fully_muted("never-seen") is True


def test_mute_level_predicates(config: BridgeConfig) -> None:
    """sync opts in fully; ring silences chatter but keeps permission ringing."""
    daemon = Daemon(config)
    daemon.set_mute_level("synced", "sync")
    daemon.set_mute_level("ringing", "ring")

    # sync = not silenced, not fully-muted → full TUI→Slack sync.
    assert not daemon.is_silenced("synced") and not daemon.is_fully_muted("synced")
    # ring = silenced (ambient gone), but NOT fully muted (permission rings).
    assert daemon.is_silenced("ringing") and not daemon.is_fully_muted("ringing")


def test_mute_invalid_level_rejected(config: BridgeConfig) -> None:
    daemon = Daemon(config)
    with pytest.raises(ValueError):
        daemon.set_mute_level("s1", "bogus")
    # "full" is no longer a level — default state replaces it.
    with pytest.raises(ValueError):
        daemon.set_mute_level("s1", "full")


async def test_mute_api_level_shape(config: BridgeConfig) -> None:
    """POST /sessions/{id}/mute accepts {level: sync|ring|none}."""
    daemon = Daemon(config)
    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/sessions/s1/mute", json={"level": "sync"})
        assert resp.status == 200
        assert (await resp.json())["level"] == "sync"
        assert daemon._mute_levels == {"s1": "sync"}

        # ring overrides sync on same session
        resp = await client.post("/sessions/s1/mute", json={"level": "ring"})
        assert (await resp.json())["level"] == "ring"
        assert daemon._mute_levels == {"s1": "ring"}

        # none drops back to default full mute (clears the dict entry)
        resp = await client.post("/sessions/s1/mute", json={"level": "none"})
        body = await resp.json()
        assert body["ok"] is True and body["level"] is None
        assert daemon._mute_levels == {}

        # Invalid payloads → 400
        resp = await client.post("/sessions/s1/mute", json={})
        assert resp.status == 400
        resp = await client.post("/sessions/s1/mute", json={"level": "bogus"})
        assert resp.status == 400
        # "full" is no longer a valid API level either
        resp = await client.post("/sessions/s1/mute", json={"level": "full"})
        assert resp.status == 400


async def test_permission_request_default_mute_falls_through(config: BridgeConfig) -> None:
    """Sessions with no explicit level default to full mute — permission-request
    returns empty body so CC's TUI dialog takes over."""
    daemon = Daemon(config)
    daemon._slack = MagicMock()
    daemon._slack.set_thread_status = AsyncMock()
    daemon._slack.post_blocks = AsyncMock(return_value="ts.1")
    daemon._session_mgr.create(
        session_id="s1", session_name="t", channel_id="C1", thread_ts="1.0",
        mode=SessionMode.HOOK,
    )
    # No set_mute_level call — default state applies.

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/permission-request", json={
            "session_key": "s1", "tool_name": "Bash",
            "tool_input": {"command": "rm file"}, "cwd": "/tmp",
        })
        assert resp.status == 200
        assert (await resp.text()) == ""
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
    daemon.set_mute_level("s1", "ring")

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


def _mock_slack_for_lazy_bind(daemon) -> None:
    """Rig up a SlackClient double that supports _ensure_slack_thread + posts."""
    fake_web = MagicMock()
    fake_web.conversations_list = AsyncMock(return_value={"channels": [{"id": "D1"}]})
    daemon._slack = MagicMock()
    daemon._slack.web = fake_web
    daemon._slack.post_blocks = AsyncMock(return_value="ts.auto")
    daemon._slack.post_text = AsyncMock()
    daemon._slack.set_thread_status = AsyncMock()
    daemon._bot_user_id = "U_BOT"


async def test_session_start_default_mute_creates_no_slack_thread(config: BridgeConfig) -> None:
    """New TUI sessions default to full mute: no DM thread, no post at all.

    Regression for: (a) _auto_bind_session AttributeError on every new session;
    (b) auto-bind posting a thread header into the DM for every fresh CC session,
    cluttering the user's Slack inbox with empty threads.
    """
    daemon = Daemon(config)
    _mock_slack_for_lazy_bind(daemon)

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/session-start", json={
            "session_key": "brand-new-sid",
            "cwd": "/tmp/project",
            "tmux_pane_id": "%0",
        })
        assert resp.status == 200

    # Session row exists (so we can track origin/tmux/mode) but has no Slack binding.
    session = daemon._session_mgr.get("brand-new-sid")
    assert session is not None
    assert session.channel_id == ""
    assert session.thread_ts == ""
    # Nothing posted to Slack.
    daemon._slack.post_blocks.assert_not_awaited()
    daemon._slack.post_text.assert_not_awaited()


async def test_sync_on_lazy_creates_thread(config: BridgeConfig) -> None:
    """/sessions/bind (sync-on) is the trigger that creates the Slack thread."""
    daemon = Daemon(config)
    _mock_slack_for_lazy_bind(daemon)

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/sessions/bind", json={
            "session_id": "sid-A", "name": "TUI-A", "cwd": "/tmp/project",
        })
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
        assert body["channel_id"] == "D1"
        assert body["thread_ts"] == "ts.auto"

    # Thread header posted exactly once.
    daemon._slack.post_blocks.assert_awaited_once()
    session = daemon._session_mgr.get("sid-A")
    assert session.channel_id == "D1" and session.thread_ts == "ts.auto"


async def test_sync_on_idempotent(config: BridgeConfig) -> None:
    """Calling /sessions/bind twice reuses the existing thread; no re-post."""
    daemon = Daemon(config)
    _mock_slack_for_lazy_bind(daemon)

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        await client.post("/sessions/bind", json={"session_id": "sid-B", "cwd": "/tmp"})
        await client.post("/sessions/bind", json={"session_id": "sid-B", "cwd": "/tmp"})

    # First call posts; second is a no-op on the Slack side.
    assert daemon._slack.post_blocks.await_count == 1


async def test_finalize_progress_chunk_failure_does_not_swallow_rest(config: BridgeConfig) -> None:
    """If one chunk's post_text raises, remaining chunks must still be attempted.

    Regression for the 'long sync response stops after first chunk' bug:
    _finalize_progress used to wrap the entire chunk loop in one try/except,
    so any Slack API hiccup on chunk N silently dropped chunks N+1..end.
    """
    daemon = Daemon(config)
    session = daemon._register_session("sid-F", "/tmp")
    session.channel_id = "D1"
    session.thread_ts = "ts.root"

    # Pretend we have a live progress message that finalize should chat_update.
    daemon._progress["sid-F"] = {
        "msg_ts": "ts.prog", "last_update": 0, "lines": [],
        "_full_text": "", "_bracket_hold": "",
    }

    # Build a display long enough to split into 3+ chunks (SLACK_MSG_LIMIT = 3900).
    long_text = ("word " * 2500)  # ~12,500 chars
    # Slack double: chat_update succeeds, post_text fails on the SECOND call
    # (i.e. first post_text after the chat_update — chunk index 1).
    call_count = {"n": 0}
    async def flaky_post_text(channel, text, thread_ts=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated Slack rate limit")
        return "ts.ok"

    fake_web = MagicMock()
    fake_web.chat_update = AsyncMock()
    daemon._slack = MagicMock()
    daemon._slack.web = fake_web
    daemon._slack.post_text = AsyncMock(side_effect=flaky_post_text)

    await daemon._finalize_progress(session, long_text)

    # chat_update ran once for chunk 0.
    fake_web.chat_update.assert_awaited_once()
    # post_text was called for every remaining chunk even though one raised —
    # the previous bug would have stopped at the first failure.
    from claude_slack_bridge.slack_formatter import split_message, md_to_mrkdwn
    expected_chunks = len(split_message(md_to_mrkdwn(long_text)))
    assert expected_chunks >= 3, "test needs a message that splits into ≥3 chunks"
    # chunks after chat_update = expected_chunks - 1
    assert daemon._slack.post_text.await_count == expected_chunks - 1


async def test_permission_request_ring_mute_lazy_binds_thread(config: BridgeConfig) -> None:
    """First permission-request under ring mute creates the thread on demand."""
    daemon = Daemon(config)
    _mock_slack_for_lazy_bind(daemon)
    # Pre-register session in ring mode (mimics /sync-ring having set the level
    # but not yet bound, e.g. if ring was set before any Slack interaction).
    session = daemon._register_session("sid-R", "/tmp/proj")
    daemon.set_mute_level("sid-R", "ring")
    assert session.channel_id == ""  # no thread yet

    # Stub out the approval wait so we don't block.
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
            "session_key": "sid-R", "tool_name": "Bash",
            "tool_input": {"command": "rm"}, "cwd": "/tmp",
        })
        body = await resp.json()
        assert body["decision"] == "approved"

    # Thread was lazy-bound during the permission-request handling.
    session = daemon._session_mgr.get("sid-R")
    assert session.channel_id == "D1" and session.thread_ts == "ts.auto"
    daemon._slack.post_blocks.assert_awaited()  # header + approval buttons


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


# ── Plan-mode sync regression (reporter: sync-on then /plan stops posting) ──


def _setup_bound_sync_session(daemon, sid: str = "sid-plan") -> None:
    """Bound HOOK-mode session with sync mute + mocked Slack. Shared by plan-mode tests."""
    session = daemon._session_mgr.create(
        session_id=sid, session_name=f"TUI-{sid}",
        channel_id="D1", thread_ts="ts.root",
        mode=SessionMode.HOOK,
    )
    session.cwd = "/tmp"
    daemon.set_mute_level(sid, "sync")  # opt in so is_silenced() is False

    fake_web = MagicMock()
    fake_web.chat_update = AsyncMock()
    daemon._slack = MagicMock()
    daemon._slack.web = fake_web
    daemon._slack.post_text = AsyncMock(return_value="ts.new")
    daemon._slack.post_blocks = AsyncMock(return_value="ts.new")
    daemon._slack.set_thread_status = AsyncMock()
    daemon._slack.update_text = AsyncMock()
    daemon._bot_user_id = "U_BOT"


async def test_user_prompt_plan_mode_system_reminder_filter_drops_real_text(
    config: BridgeConfig,
) -> None:
    """H1 regression: plan-mode prepends <system-reminder> to every user prompt;
    the user-prompt handler now strips that leading wrapper and still syncs the
    real text. Before the fix, daemon_http.py's startswith("<") + tag-in-100-chars
    heuristic dropped the whole payload.
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-plan")

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    real_text = "Hey, actually write the code"
    prompt = (
        "<system-reminder>\nPlan mode is active. The user indicated "
        "that they do not want you to execute yet.\n</system-reminder>\n\n"
        + real_text
    )

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/user-prompt", json={
            "session_key": "sid-plan", "prompt": prompt, "cwd": "/tmp",
        })
        assert resp.status == 200

    # The real user text must be synced to Slack. Current code filters it out.
    post_calls = daemon._slack.post_text.await_args_list
    texts = [c.args[1] for c in post_calls]
    assert any(real_text in t for t in texts), (
        f"Real user text was never posted to Slack. post_text calls: {texts!r}"
    )


async def test_user_prompt_bare_system_reminder_still_filtered(
    config: BridgeConfig,
) -> None:
    """H1 guard: a prompt that is *only* a system-reminder (no real user text)
    must stay filtered, so the fix doesn't leak internal scaffolding into Slack.
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-bare")

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    prompt = "<system-reminder>internal reminder only</system-reminder>"

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/user-prompt", json={
            "session_key": "sid-bare", "prompt": prompt, "cwd": "/tmp",
        })
        assert resp.status == 200

    # Must NOT post the reminder as a user message.
    for call in daemon._slack.post_text.await_args_list:
        assert "internal reminder only" not in call.args[1]


async def test_user_prompt_pops_progress_even_when_filtered(
    config: BridgeConfig,
) -> None:
    """H2 probe: user-prompt's _progress.pop runs before the filter, so a
    filtered plan-mode prompt still resets progress state — which means a
    *subsequent* post-tool-use creates a fresh message, NOT editing the old one.

    If this assertion ever flips, the pop has been moved inside the filter
    and H2 becomes the new explanation for "edits previous message".
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-pop")

    # Seed stale progress state from a prior (unfinalized) turn.
    daemon._progress["sid-pop"] = {
        "msg_ts": "ts.old", "last_update": 0, "lines": [],
        "_text_blocks": [], "_tool": "",
    }

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    prompt = "<system-reminder>Plan mode is active.</system-reminder>"  # filtered

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/user-prompt", json={
            "session_key": "sid-pop", "prompt": prompt, "cwd": "/tmp",
        })
        assert resp.status == 200

    # Pop at daemon_http.py:361 runs unconditionally (before the filter).
    assert "sid-pop" not in daemon._progress, (
        "user-prompt handler should clear stale _progress before filtering"
    )


async def test_post_tool_use_does_not_touch_progress_message(
    config: BridgeConfig,
) -> None:
    """Invariant: the post-tool-use hook must NOT write the progress message.
    Tool display is driven exclusively by the JSONL watcher (single content
    source, prevents double-append). The hook still does status side-effects
    — phase reaction, thread status, pending-approval cleanup — but stays
    out of _update_progress.

    Regression guard: an earlier version of the hook called
    _update_progress(is_tool=True) directly, which caused every tool to
    appear twice once we made the watcher the content source, and caused
    stale-msg_ts edits when a turn boundary dropped the user-prompt hook.
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-notouch")

    # Seed progress state as if a prior turn were still in flight.
    daemon._progress["sid-notouch"] = {
        "msg_ts": "ts.old", "last_update": 0, "lines": [],
        "_text_blocks": ["prior assistant text"], "_tool": "",
    }

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/post-tool-use", json={
            "session_key": "sid-notouch",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_output": "",
            "cwd": "/tmp",
        })
        assert resp.status == 200

    # Hook did side-effects (set_thread_status), but did NOT chat_update
    # the progress message.
    daemon._slack.web.chat_update.assert_not_awaited()
    daemon._slack.set_thread_status.assert_awaited()


# ── _strip_wrapper_blocks unit tests ──


def test_strip_wrapper_keeps_real_text_after_system_reminder():
    """The plan-mode case: one leading <system-reminder> block + real user text."""
    got = _strip_wrapper_blocks(
        "<system-reminder>Plan mode is active.</system-reminder>\n\nfix the bug"
    )
    assert got == "fix the bug"


def test_strip_wrapper_peels_multiple_leading_blocks():
    """Slash-command invocations stack several wrapper blocks before real text."""
    got = _strip_wrapper_blocks(
        "<command-name>/plan</command-name>"
        "<command-message>plan</command-message>"
        "<command-args></command-args>\n\n"
        "design the migration"
    )
    assert got == "design the migration"


def test_strip_wrapper_returns_empty_for_wrapper_only():
    """Pure wrapper payload → caller treats as skip (nothing to sync)."""
    assert _strip_wrapper_blocks(
        "<system-reminder>internal only</system-reminder>"
    ) == ""


def test_strip_wrapper_leaves_plain_text_untouched():
    """No wrappers: stripper is a no-op (sans outer whitespace)."""
    assert _strip_wrapper_blocks("  hello world  ") == "hello world"


def test_strip_wrapper_does_not_strip_inline_lookalikes():
    """<system-reminder> in the middle of a real prompt stays put — we only peel
    leading wrappers, not anything that happens to contain angle brackets."""
    got = _strip_wrapper_blocks(
        "look at <system-reminder>this literal</system-reminder> in the code"
    )
    assert got == "look at <system-reminder>this literal</system-reminder> in the code"


def test_strip_wrapper_handles_multiline_reminder():
    """The real plan-mode payload has newlines and a big block of rules."""
    prompt = (
        "<system-reminder>\n"
        "Plan mode is active. The user indicated that they do not want you to\n"
        "execute yet -- you MUST NOT make any edits...\n"
        "</system-reminder>\n\n"
        "implement the feature"
    )
    assert _strip_wrapper_blocks(prompt) == "implement the feature"


# ── Progress accumulation (B1) and approval sealing ──


async def test_update_progress_accumulates_assistant_text(config: BridgeConfig) -> None:
    """Successive assistant text blocks should stack in the progress message
    instead of overwriting each other. The old single-slot behavior made long
    reasoning look like "one sentence, then suddenly the final answer."
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-acc")
    # Drive updates past the 1s throttle between calls.
    import time as _t

    await daemon._update_progress(daemon._session_mgr.get("sid-acc"), "_first thought_")
    state = daemon._progress["sid-acc"]
    state["last_update"] = 0  # let the next update fire chat_update
    await daemon._update_progress(daemon._session_mgr.get("sid-acc"), "_second thought_")

    # Both blocks must survive in state.
    assert daemon._progress["sid-acc"]["_text_blocks"] == [
        "_first thought_", "_second thought_"
    ]
    # The chat_update payload should contain both.
    chat_update = daemon._slack.web.chat_update
    chat_update.assert_awaited()
    last_text = chat_update.await_args.kwargs.get("text", "")
    assert "_first thought_" in last_text and "_second thought_" in last_text


async def test_update_progress_archives_previous_tool_into_history(
    config: BridgeConfig,
) -> None:
    """When a new tool line arrives, the previous tool line graduates into
    _text_blocks rather than being dropped. This keeps the full tool timeline
    visible in the progress message (matches TUI's interleaved thinking+tools).
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-tools")
    session = daemon._session_mgr.get("sid-tools")

    # Tool 1: seeds _tool slot.
    await daemon._update_progress(session, "🪆 `Read` foo.py", is_tool=True)
    state = daemon._progress["sid-tools"]
    state["last_update"] = 0
    assert state["_tool"] == "🪆 `Read` foo.py"
    assert state["_text_blocks"] == []

    # Tool 2 arrives: tool 1 should be archived into history.
    await daemon._update_progress(session, "🪆 `Bash` ls", is_tool=True)
    assert daemon._progress["sid-tools"]["_text_blocks"] == [
        "🪆 `Read` foo.py"
    ]
    assert daemon._progress["sid-tools"]["_tool"] == "🪆 `Bash` ls"

    # A thought interleaves, then tool 3 — both prior tools now in history.
    daemon._progress["sid-tools"]["last_update"] = 0
    await daemon._update_progress(session, "● _hmm_")
    daemon._progress["sid-tools"]["last_update"] = 0
    await daemon._update_progress(session, "🪆 `Grep` pattern", is_tool=True)
    assert daemon._progress["sid-tools"]["_text_blocks"] == [
        "🪆 `Read` foo.py",
        "● _hmm_",
        "🪆 `Bash` ls",
    ]
    assert daemon._progress["sid-tools"]["_tool"] == "🪆 `Grep` pattern"


async def test_update_progress_no_200_char_truncation(config: BridgeConfig) -> None:
    """The watcher used to truncate assistant text to 200 chars — verify a
    longer block survives end-to-end through _update_progress.
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-long")
    long_block = "_" + ("word " * 80).strip() + "_"  # ~400 chars
    assert len(long_block) > 200

    await daemon._update_progress(daemon._session_mgr.get("sid-long"), long_block)
    state = daemon._progress["sid-long"]
    state["last_update"] = 0
    await daemon._update_progress(daemon._session_mgr.get("sid-long"), "_next_")

    text = daemon._slack.web.chat_update.await_args.kwargs.get("text", "")
    assert long_block in text


async def test_seal_progress_clears_state_and_drops_cursor(config: BridgeConfig) -> None:
    """_seal_progress should: (a) remove the session's entry from _progress so
    the next update creates a fresh Slack message, (b) chat_update the frozen
    copy one last time without the streaming cursor.
    """
    from claude_slack_bridge.daemon_stream import _CURSOR

    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-seal")
    session = daemon._session_mgr.get("sid-seal")
    daemon._progress["sid-seal"] = {
        "msg_ts": "ts.live", "last_update": 0, "lines": [],
        "_text_blocks": ["thinking..."], "_tool": "🪆 `Bash` ls",
    }

    await daemon._seal_progress(session)

    # State popped — next _update_progress would start fresh.
    assert "sid-seal" not in daemon._progress
    # Frozen redraw landed and did NOT carry the cursor marker.
    chat_update = daemon._slack.web.chat_update
    chat_update.assert_awaited_once()
    kwargs = chat_update.await_args.kwargs
    assert kwargs["ts"] == "ts.live"
    assert _CURSOR not in kwargs["text"]
    assert "thinking..." in kwargs["text"]
    assert "Bash" in kwargs["text"]


async def test_seal_progress_is_noop_when_no_active_progress(config: BridgeConfig) -> None:
    """Sealing when there's no in-flight progress must not crash and must not
    spam Slack with an empty chat_update (e.g. approval as the very first
    event of a turn)."""
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-noseal")
    session = daemon._session_mgr.get("sid-noseal")

    await daemon._seal_progress(session)  # should just return

    daemon._slack.web.chat_update.assert_not_awaited()


async def test_permission_request_seals_progress_before_posting_buttons(
    config: BridgeConfig,
) -> None:
    """When a TUI permission-request arrives with a live progress message,
    the approval buttons must come AFTER a sealed copy of the current progress.
    Observable: _progress[sid] is cleared by the time post_blocks is called,
    and the seal's chat_update fired before the buttons' post_blocks.
    """
    daemon = Daemon(config)
    _setup_bound_sync_session(daemon, "sid-perm")

    # Seed live progress state.
    daemon._progress["sid-perm"] = {
        "msg_ts": "ts.live", "last_update": 0, "lines": [],
        "_text_blocks": ["I should probably run this"], "_tool": "",
    }

    # Track call ordering: chat_update (seal) must happen before post_blocks (buttons).
    call_order: list[str] = []
    orig_chat_update = daemon._slack.web.chat_update
    async def tracked_chat_update(**kw):
        call_order.append("seal")
        return await orig_chat_update(**kw)
    daemon._slack.web.chat_update = tracked_chat_update

    orig_post_blocks = daemon._slack.post_blocks
    async def tracked_post_blocks(*a, **kw):
        call_order.append("buttons")
        return await orig_post_blocks(*a, **kw)
    daemon._slack.post_blocks = tracked_post_blocks

    # Stub approval wait so it doesn't block.
    original_create = daemon._approval_mgr.create
    def _fake_create(request_id, **kwargs):
        st = original_create(request_id, **kwargs)
        st.resolve("approved")
        return st
    daemon._approval_mgr.create = _fake_create

    app = create_http_app(daemon)
    from aiohttp.test_utils import TestServer, TestClient

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/hooks/permission-request", json={
            "session_key": "sid-perm",
            "tool_name": "Bash",
            "tool_input": {"command": "rm file"},
            "cwd": "/tmp",
        })
        assert resp.status == 200

    # Progress state was sealed → no longer tracked.
    assert "sid-perm" not in daemon._progress
    # Seal ran, then buttons were posted.
    assert "seal" in call_order and "buttons" in call_order
    assert call_order.index("seal") < call_order.index("buttons"), (
        f"Approval buttons must land after the sealed progress; got {call_order}"
    )
