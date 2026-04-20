from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class ApprovalState:
    def __init__(
        self,
        request_id: str,
        tool_name: str = "",
        tool_input: dict | None = None,
        cwd: str = "",
        session_id: str = "",
    ) -> None:
        self.request_id = request_id
        self.tool_name = tool_name
        self.tool_input = tool_input or {}
        self.cwd = cwd
        self.session_id = session_id
        self.status: str = "pending"  # pending | approved | trusted | rejected | timed_out
        self.trust_tool_name: str = ""
        self.trust_rule_content: str | None = None
        self.trust_destination: str = "projectSettings"
        self._event = asyncio.Event()
        self._resolved = False

    def resolve(
        self,
        decision: str,
        *,
        trust_tool_name: str = "",
        trust_rule_content: str | None = None,
        trust_destination: str = "projectSettings",
    ) -> bool:
        """Atomically resolve. Returns True if this was the first resolution."""
        if self._resolved:
            return False
        self._resolved = True
        self.status = decision
        if decision == "trusted":
            self.trust_tool_name = trust_tool_name
            self.trust_rule_content = trust_rule_content
            self.trust_destination = trust_destination
        self._event.set()
        return True

    async def wait(self, timeout: float | None = None) -> str:
        """Wait for resolution. Returns status string."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.resolve("timed_out")
        return self.status


class ApprovalManager:
    def __init__(self) -> None:
        self._pending: dict[str, ApprovalState] = {}

    def create(
        self,
        request_id: str,
        tool_name: str = "",
        tool_input: dict | None = None,
        cwd: str = "",
        session_id: str = "",
    ) -> ApprovalState:
        state = ApprovalState(
            request_id,
            tool_name=tool_name,
            tool_input=tool_input,
            cwd=cwd,
            session_id=session_id,
        )
        self._pending[request_id] = state
        return state

    def resolve(self, request_id: str, decision: str) -> bool:
        state = self._pending.get(request_id)
        if state is None:
            logger.warning("No pending approval for request_id=%s", request_id)
            return False
        return state.resolve(decision)

    def get(self, request_id: str) -> ApprovalState | None:
        return self._pending.get(request_id)

    def cleanup(self, request_id: str) -> None:
        self._pending.pop(request_id, None)
