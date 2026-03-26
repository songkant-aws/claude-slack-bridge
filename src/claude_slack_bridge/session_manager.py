from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class SessionMode(str, Enum):
    PROCESS = "process"  # daemon manages claude --print
    HOOK = "hook"        # TUI active, hooks push to Slack
    IDLE = "idle"        # neither active


@dataclass
class Session:
    session_id: str  # Claude Code UUID
    session_name: str
    channel_id: str
    thread_ts: str | None
    mode: str = SessionMode.IDLE.value
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    status: str = "active"
    cwd: str = ""  # working directory for claude processes
    tui_active: float = 0  # timestamp of last TUI hook activity

    def touch(self) -> None:
        self.last_active = time.time()


class SessionManager:
    def __init__(self, storage_path: Path) -> None:
        self._path = storage_path
        self._sessions: dict[str, Session] = {}
        # Reverse index: (channel_id, thread_ts) -> session_id
        self._thread_index: dict[tuple[str, str], str] = {}
        if self._path.is_file():
            self._load()

    def create(
        self,
        session_id: str,
        session_name: str,
        channel_id: str,
        thread_ts: str | None,
        mode: SessionMode = SessionMode.PROCESS,
    ) -> Session:
        s = Session(
            session_id=session_id,
            session_name=session_name,
            channel_id=channel_id,
            thread_ts=thread_ts,
            mode=mode.value,
        )
        self._sessions[session_id] = s
        if thread_ts:
            self._thread_index[(channel_id, thread_ts)] = session_id
        self._save()
        return s

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def find_by_thread(self, channel_id: str, thread_ts: str) -> Session | None:
        sid = self._thread_index.get((channel_id, thread_ts))
        if sid:
            return self._sessions.get(sid)
        # Fallback: linear scan
        for s in self._sessions.values():
            if s.status == "active" and s.channel_id == channel_id and s.thread_ts == thread_ts:
                return s
        return None

    def set_mode(self, session_id: str, mode: SessionMode) -> None:
        if s := self._sessions.get(session_id):
            s.mode = mode.value
            s.touch()
            self._save()

    def list_active(self) -> list[Session]:
        return [s for s in self._sessions.values() if s.status == "active"]

    def archive(self, session_id: str) -> None:
        if s := self._sessions.get(session_id):
            s.status = "archived"
            if s.thread_ts:
                self._thread_index.pop((s.channel_id, s.thread_ts), None)
            self._save()

    def cleanup(self, max_idle_secs: int = 86400) -> list[str]:
        now = time.time()
        archived = []
        for sid, s in self._sessions.items():
            if s.status == "active" and (now - s.last_active) > max_idle_secs:
                s.status = "archived"
                archived.append(sid)
        if archived:
            self._rebuild_index()
            self._save()
        return archived

    def _rebuild_index(self) -> None:
        self._thread_index = {}
        for s in self._sessions.values():
            if s.status == "active" and s.thread_ts:
                self._thread_index[(s.channel_id, s.thread_ts)] = s.session_id

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {sid: asdict(s) for sid, s in self._sessions.items()}
        self._path.write_text(json.dumps(data, indent=2))

    def _load(self) -> None:
        raw = json.loads(self._path.read_text())
        for sid, d in raw.items():
            # Compat: drop unknown fields, add defaults for new fields
            known = {f.name for f in Session.__dataclass_fields__.values()}
            clean = {k: v for k, v in d.items() if k in known}
            clean.setdefault("mode", SessionMode.IDLE.value)
            self._sessions[sid] = Session(**clean)
        self._rebuild_index()
