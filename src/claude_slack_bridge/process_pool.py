"""Manage Claude Code --print subprocesses."""
from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from claude_slack_bridge.stream_parser import StreamEvent, parse_line

logger = logging.getLogger(__name__)


@dataclass
class ClaudeProcess:
    session_id: str
    process: asyncio.subprocess.Process
    _reader_task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def alive(self) -> bool:
        return self.process.returncode is None

    async def send_message(self, text: str) -> None:
        """Send a user message via stdin (stream-json input format)."""
        if not self.alive or not self.process.stdin:
            logger.warning("Cannot send to dead process %s", self.session_id)
            return
        import json
        line = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": text},
        }) + "\n"
        self.process.stdin.write(line.encode())
        await self.process.stdin.drain()

    async def terminate(self) -> None:
        """Gracefully terminate the process."""
        if not self.alive:
            return
        self.process.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(self.process.wait(), timeout=5)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()
        if self._reader_task:
            self._reader_task.cancel()


# Callback type: called for each stream event
OnEvent = Callable[[str, StreamEvent], Coroutine[Any, Any, None]]
# Callback type: called when process exits
OnExit = Callable[[str, int | None], Coroutine[Any, Any, None]]


class ProcessPool:
    def __init__(self) -> None:
        self._processes: dict[str, ClaudeProcess] = {}

    async def start(
        self,
        session_id: str,
        prompt: str | None = None,
        resume: bool = False,
        name: str | None = None,
        cwd: str | None = None,
        extra_args: list[str] | None = None,
        on_event: OnEvent | None = None,
        on_exit: OnExit | None = None,
    ) -> ClaudeProcess:
        """Start a claude --print process for a session."""
        # Kill existing process for this session
        if session_id in self._processes:
            logger.info("Killing existing process for session %s", session_id)
            await self._processes[session_id].terminate()

        cmd = [
            "claude", "--print",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--session-id", session_id,
            "--setting-sources", "project,local",  # Skip user settings (hooks)
        ]
        if resume:
            cmd += ["--resume", session_id]
        if name:
            cmd += ["--name", name]
        if extra_args:
            cmd += extra_args

        logger.info("Starting claude process: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )

        cp = ClaudeProcess(session_id=session_id, process=proc)
        self._processes[session_id] = cp

        # Start stdout reader
        cp._reader_task = asyncio.create_task(
            self._read_stdout(cp, on_event, on_exit)
        )

        # Wait for init event before sending first message
        if prompt:
            await asyncio.sleep(2)
            if cp.alive:
                await cp.send_message(prompt)
            else:
                logger.error("Claude process died before sending prompt")

        return cp

    async def _read_stdout(
        self,
        cp: ClaudeProcess,
        on_event: OnEvent | None,
        on_exit: OnExit | None,
    ) -> None:
        """Read stdout line by line, parse events, invoke callback."""
        try:
            while cp.process.stdout:
                line = await cp.process.stdout.readline()
                if not line:
                    break
                evt = parse_line(line.decode("utf-8", errors="replace"))
                if evt and on_event:
                    try:
                        await on_event(cp.session_id, evt)
                    except Exception:
                        logger.exception("Error in on_event callback")
        except asyncio.CancelledError:
            return
        finally:
            rc = await cp.process.wait()
            logger.info("Claude process %s exited with code %s", cp.session_id, rc)
            self._processes.pop(cp.session_id, None)
            if on_exit:
                try:
                    await on_exit(cp.session_id, rc)
                except Exception:
                    logger.exception("Error in on_exit callback")

    def get(self, session_id: str) -> ClaudeProcess | None:
        cp = self._processes.get(session_id)
        if cp and cp.alive:
            return cp
        return None

    async def terminate(self, session_id: str) -> None:
        if cp := self._processes.get(session_id):
            await cp.terminate()

    async def terminate_all(self) -> None:
        for cp in list(self._processes.values()):
            await cp.terminate()
