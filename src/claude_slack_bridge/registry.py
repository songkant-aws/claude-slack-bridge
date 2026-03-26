from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class SessionMapping:
    session_id: str
    session_name: str
    channel_id: str
    thread_ts: str | None
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    is_dedicated_channel: bool = False
    status: str = "active"


class SessionRegistry:
    def __init__(self, storage_path: Path) -> None:
        self._path = storage_path
        self._sessions: dict[str, SessionMapping] = {}
        if self._path.is_file():
            self.load()

    def register(
        self,
        session_id: str,
        session_name: str,
        channel_id: str,
        thread_ts: str | None,
    ) -> SessionMapping:
        now = time.time()
        mapping = SessionMapping(
            session_id=session_id,
            session_name=session_name,
            channel_id=channel_id,
            thread_ts=thread_ts,
            created_at=now,
            last_active=now,
        )
        self._sessions[session_id] = mapping
        self.save()
        return mapping

    def get(self, session_id: str) -> SessionMapping | None:
        return self._sessions.get(session_id)

    def touch(self, session_id: str) -> None:
        if m := self._sessions.get(session_id):
            m.last_active = time.time()

    def promote(self, session_id: str, new_channel_id: str) -> None:
        if m := self._sessions.get(session_id):
            m.channel_id = new_channel_id
            m.thread_ts = None
            m.is_dedicated_channel = True
            self.save()

    def archive(self, session_id: str) -> None:
        if m := self._sessions.get(session_id):
            m.status = "archived"
            self.save()

    def find_by_thread(self, channel_id: str, thread_ts: str) -> SessionMapping | None:
        """Reverse-lookup: find active session by channel + thread_ts."""
        for m in self._sessions.values():
            if m.status == "active" and m.channel_id == channel_id and m.thread_ts == thread_ts:
                return m
        return None

    def list_active(self) -> list[SessionMapping]:
        return [m for m in self._sessions.values() if m.status == "active"]

    def cleanup(self, max_idle_secs: int = 86400) -> list[str]:
        now = time.time()
        archived: list[str] = []
        for sid, m in self._sessions.items():
            if m.status == "active" and (now - m.last_active) > max_idle_secs:
                m.status = "archived"
                archived.append(sid)
        if archived:
            self.save()
        return archived

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {sid: asdict(m) for sid, m in self._sessions.items()}
        self._path.write_text(json.dumps(data, indent=2))

    def load(self) -> None:
        if not self._path.is_file():
            return
        raw = json.loads(self._path.read_text())
        self._sessions = {}
        known = {f.name for f in SessionMapping.__dataclass_fields__.values()}
        for sid, d in raw.items():
            clean = {k: v for k, v in d.items() if k in known}
            self._sessions[sid] = SessionMapping(**clean)
