"""Tests for plugins/slack-bridge/bin/claude-slack-bridge-session-id.

The resolver is a standalone stdlib script (no .py extension) so we load it
via importlib rather than a normal import. It walks the parent-process chain
reading ~/.claude/sessions/<pid>.json; tests stub `_ppid` and `Path.home`
instead of touching /proc.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "slack-bridge"
    / "bin"
    / "claude-slack-bridge-session-id"
)


@pytest.fixture
def resolver() -> ModuleType:
    # The script has no .py extension, so we supply a SourceFileLoader
    # explicitly — spec_from_file_location won't infer it otherwise.
    loader = SourceFileLoader("_cc_session_id_resolver", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point Path.home() at tmp_path and create ~/.claude/sessions/."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".claude" / "sessions").mkdir(parents=True)
    return tmp_path


def _write_session(home: Path, pid: int, **fields: object) -> None:
    payload = {"pid": pid, "sessionId": f"sid-{pid}", "kind": "interactive"}
    payload.update(fields)
    (home / ".claude" / "sessions" / f"{pid}.json").write_text(json.dumps(payload))


def _patch_parent_chain(
    monkeypatch: pytest.MonkeyPatch, resolver: ModuleType, chain: list[int]
) -> None:
    """Make os.getpid() return chain[0] and _ppid() walk chain[1:]."""
    it = iter(chain)
    monkeypatch.setattr(resolver.os, "getpid", lambda: next(it))
    parents = dict(zip(chain, chain[1:]))
    monkeypatch.setattr(resolver, "_ppid", lambda pid: parents.get(pid))


def test_resolves_via_parent_chain(
    fake_home: Path, resolver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_session(fake_home, 4242, cwd="/proj/a", sessionId="real-sid")
    _patch_parent_chain(monkeypatch, resolver, [100, 200, 4242, 1])

    assert resolver.resolve_session_id("/proj/a") == "real-sid"


def test_skips_mismatched_cwd(
    fake_home: Path, resolver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nearest ancestor is a session for a DIFFERENT cwd — resolver must skip it
    # and keep walking rather than claim that session.
    _write_session(fake_home, 500, cwd="/proj/other", sessionId="wrong")
    _write_session(fake_home, 900, cwd="/proj/mine", sessionId="right")
    _patch_parent_chain(monkeypatch, resolver, [100, 500, 900, 1])

    assert resolver.resolve_session_id("/proj/mine") == "right"


def test_skips_non_interactive_kind(
    fake_home: Path, resolver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-interactive `claude --print` subprocess in the chain (e.g. the
    # daemon's own process pool) must not be picked up.
    _write_session(fake_home, 500, cwd="/proj/a", sessionId="print-sid", kind="print")
    _write_session(fake_home, 900, cwd="/proj/a", sessionId="tui-sid")
    _patch_parent_chain(monkeypatch, resolver, [100, 500, 900, 1])

    assert resolver.resolve_session_id("/proj/a") == "tui-sid"


def test_returns_none_when_sessions_dir_missing(
    tmp_path: Path, resolver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # Deliberately do not create ~/.claude/sessions
    assert resolver.resolve_session_id("/proj/a") is None


def test_returns_none_when_chain_has_no_session_file(
    fake_home: Path, resolver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_parent_chain(monkeypatch, resolver, [100, 200, 300, 1])
    assert resolver.resolve_session_id("/proj/a") is None


def test_walk_is_bounded(
    fake_home: Path, resolver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _ppid returns a fresh pid forever; bound must stop the walk at 32 hops.
    counter = {"n": 1000}

    def fake_ppid(_pid: int) -> int:
        counter["n"] += 1
        return counter["n"]

    monkeypatch.setattr(resolver.os, "getpid", lambda: 999)
    monkeypatch.setattr(resolver, "_ppid", fake_ppid)

    assert resolver.resolve_session_id("/proj/a") is None
    # Proves we bailed out instead of looping forever; exact bound is <= 32.
    assert counter["n"] < 1000 + 64


def test_skips_corrupt_json_and_continues(
    fake_home: Path, resolver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_home / ".claude" / "sessions" / "500.json").write_text("{ not json")
    _write_session(fake_home, 900, cwd="/proj/a", sessionId="good")
    _patch_parent_chain(monkeypatch, resolver, [100, 500, 900, 1])

    assert resolver.resolve_session_id("/proj/a") == "good"


def test_ignores_ppid_eq_1(
    fake_home: Path, resolver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Once we reach init (pid 1) the walk must stop even if a stale
    # /1.json happens to exist (belt-and-braces against PID reuse quirks).
    _write_session(fake_home, 1, cwd="/proj/a", sessionId="init-ghost")
    _patch_parent_chain(monkeypatch, resolver, [100, 1])

    assert resolver.resolve_session_id("/proj/a") is None


def test_main_prints_sid_and_exits_zero(
    fake_home: Path,
    resolver: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_session(fake_home, 777, cwd="/proj/a", sessionId="abc")
    _patch_parent_chain(monkeypatch, resolver, [100, 777, 1])
    monkeypatch.setattr(resolver.sys, "argv", ["resolver", "/proj/a"])

    assert resolver.main() == 0
    assert capsys.readouterr().out.strip() == "abc"


def test_main_exits_one_on_failure(
    fake_home: Path,
    resolver: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_parent_chain(monkeypatch, resolver, [100, 200, 1])
    monkeypatch.setattr(resolver.sys, "argv", ["resolver", "/proj/a"])

    assert resolver.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    # Diagnostics go to stderr unconditionally on failure so users
    # filing bugs have a trace to attach.
    assert "[resolver]" in captured.err
    assert "/proj/a" in captured.err


def test_matches_cwd_via_realpath(
    fake_home: Path,
    tmp_path: Path,
    resolver: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Claude records cwd as /real/path; $PWD comes in as /symlink/path.
    # The resolver should treat them as equal after realpath.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    _write_session(fake_home, 777, cwd=str(real), sessionId="via-link")
    _patch_parent_chain(monkeypatch, resolver, [100, 777, 1])

    assert resolver.resolve_session_id(str(link)) == "via-link"


def test_trace_logs_each_candidate(
    fake_home: Path, resolver: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_session(fake_home, 500, cwd="/proj/other", sessionId="wrong-cwd")
    _write_session(fake_home, 700, kind="print", sessionId="wrong-kind")
    _write_session(fake_home, 900, cwd="/proj/a", sessionId="right")
    _patch_parent_chain(monkeypatch, resolver, [100, 500, 700, 900, 1])

    trace: list[str] = []
    assert resolver.resolve_session_id("/proj/a", trace=trace) == "right"
    blob = "\n".join(trace)
    assert "cwd mismatch" in blob  # pid=500 branch
    assert "kind='print'" in blob or 'kind="print"' in blob  # pid=700 branch
    assert "matched sessionId=right" in blob  # pid=900 branch
