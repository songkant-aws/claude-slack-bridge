from __future__ import annotations

import logging
import uuid
from pathlib import Path

from aiohttp import web

from claude_slack_bridge.approval import ApprovalManager
from claude_slack_bridge.config import BridgeConfig
from claude_slack_bridge.registry import SessionRegistry
from claude_slack_bridge.slack_client import SlackClient
from claude_slack_bridge.slack_formatter import (
    build_approval_blocks,
    build_approval_resolved_blocks,
    build_post_tool_blocks,
    build_response_blocks,
    build_session_header_blocks,
    build_user_prompt_blocks,
)

logger = logging.getLogger(__name__)

# Cache: channel name -> channel ID
_channel_id_cache: dict[str, str] = {}


async def _resolve_channel_id(slack: SlackClient, channel_name: str) -> str:
    """Resolve channel name to ID. Caches the result.
    If channel_name already looks like a Slack ID (starts with C/G), return as-is.
    """
    if channel_name.startswith(("C", "G", "D")) and " " not in channel_name:
        return channel_name
    if channel_name in _channel_id_cache:
        return _channel_id_cache[channel_name]
    name = channel_name.lstrip("#")
    resp = await slack.web.conversations_list(types="public_channel", limit=200)
    for ch in resp.get("channels", []):
        if ch["name"] == name:
            _channel_id_cache[channel_name] = ch["id"]
            return ch["id"]
    raise ValueError(
        f"Channel '{channel_name}' not found. Create it in Slack first, "
        f"or set default_channel to a channel ID (C...) in config.json."
    )


async def _ensure_session(
    registry: SessionRegistry,
    slack: SlackClient,
    config: BridgeConfig,
    session_key: str,
    cwd: str,
) -> None:
    """Auto-register session if not yet known."""
    if registry.get(session_key) is not None:
        registry.touch(session_key)
        return

    session_name = Path(cwd).name if cwd else session_key

    channel_id = await _resolve_channel_id(slack, config.default_channel)
    blocks = build_session_header_blocks(session_id=session_key, directory=cwd)
    thread_ts = await slack.post_blocks(channel_id, blocks, f"New session: {session_key}")

    registry.register(
        session_id=session_key,
        session_name=session_name,
        channel_id=channel_id,
        thread_ts=thread_ts,
    )
    logger.info("Auto-registered session %s -> thread %s", session_key, thread_ts)


def _get_target(registry: SessionRegistry, session_key: str) -> tuple[str, str | None]:
    """Return (channel_id, thread_ts) for a session."""
    mapping = registry.get(session_key)
    if mapping is None:
        raise ValueError(f"Session {session_key} not registered")
    return mapping.channel_id, mapping.thread_ts


def create_app(
    config: BridgeConfig,
    registry: SessionRegistry,
    approval_mgr: ApprovalManager,
    slack: SlackClient,
) -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/health")
    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    @routes.get("/sessions")
    async def list_sessions(request: web.Request) -> web.Response:
        active = registry.list_active()
        data = [
            {
                "session_id": m.session_id,
                "session_name": m.session_name,
                "channel_id": m.channel_id,
                "thread_ts": m.thread_ts,
                "is_dedicated_channel": m.is_dedicated_channel,
            }
            for m in active
        ]
        return web.json_response(data)

    @routes.post("/sessions/{session_id}/promote")
    async def promote_session(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        mapping = registry.get(session_id)
        if mapping is None:
            return web.json_response({"error": "session not found"}, status=404)

        name = f"cc-{mapping.session_name[:16]}"
        channel_id, created = await slack.create_channel(name)
        registry.promote(session_id, channel_id)
        return web.json_response({"channel_id": channel_id, "created": created})

    @routes.post("/hooks/pre-tool-use")
    async def pre_tool_use(request: web.Request) -> web.Response:
        payload = await request.json()
        session_key = payload["session_key"]
        cwd = payload.get("cwd", "")
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input", {})

        await _ensure_session(registry, slack, config, session_key, cwd)

        # Auto-approve if tool in whitelist or approval disabled
        if not config.require_approval or tool_name in config.auto_approve_tools:
            channel_id, thread_ts = _get_target(registry, session_key)
            blocks = build_post_tool_blocks(tool_name, tool_input, "(auto-approved)", 0)
            await slack.post_blocks(channel_id, blocks, f"Auto-approved: {tool_name}", thread_ts)
            return web.Response(text="approved")

        # Create approval and post to Slack
        request_id = payload.get("request_id", str(uuid.uuid4()))
        state = approval_mgr.create(request_id)

        channel_id, thread_ts = _get_target(registry, session_key)
        mapping = registry.get(session_key)
        session_name = mapping.session_name if mapping else session_key

        blocks = build_approval_blocks(
            tool_name, tool_input, session_key, session_name, request_id
        )
        msg_ts = await slack.post_blocks(
            channel_id, blocks, f"Approve {tool_name}?", thread_ts
        )

        # Store msg location for button callback to find
        state.channel_id = channel_id  # type: ignore[attr-defined]
        state.msg_ts = msg_ts  # type: ignore[attr-defined]
        state.tool_name = tool_name  # type: ignore[attr-defined]

        # Block until resolved
        decision = await state.wait(timeout=config.approval_timeout_secs)

        # Update Slack message to show resolution
        resolved_blocks = build_approval_resolved_blocks(tool_name, decision, request_id)
        try:
            await slack.update_blocks(channel_id, msg_ts, resolved_blocks)
        except Exception:
            logger.warning("Failed to update approval message", exc_info=True)

        approval_mgr.cleanup(request_id)
        return web.Response(text=decision if decision != "timed_out" else "approved")

    @routes.post("/hooks/post-tool-use")
    async def post_tool_use(request: web.Request) -> web.Response:
        payload = await request.json()
        session_key = payload["session_key"]
        cwd = payload.get("cwd", "")

        await _ensure_session(registry, slack, config, session_key, cwd)

        channel_id, thread_ts = _get_target(registry, session_key)
        blocks = build_post_tool_blocks(
            tool_name=payload.get("tool_name", ""),
            tool_input=payload.get("tool_input", {}),
            output=payload.get("tool_output", ""),
            duration_ms=payload.get("duration_ms", 0),
        )
        await slack.post_blocks(channel_id, blocks, "Tool result", thread_ts)
        return web.Response(text="ok")

    @routes.post("/hooks/user-prompt")
    async def user_prompt(request: web.Request) -> web.Response:
        payload = await request.json()
        session_key = payload["session_key"]
        cwd = payload.get("cwd", "")

        await _ensure_session(registry, slack, config, session_key, cwd)

        channel_id, thread_ts = _get_target(registry, session_key)
        blocks = build_user_prompt_blocks(payload.get("prompt", ""))
        await slack.post_blocks(channel_id, blocks, "User prompt", thread_ts)
        return web.Response(text="ok")

    @routes.post("/hooks/stop")
    async def stop(request: web.Request) -> web.Response:
        payload = await request.json()
        session_key = payload["session_key"]
        cwd = payload.get("cwd", "")

        await _ensure_session(registry, slack, config, session_key, cwd)

        response_text = payload.get("response", "")
        channel_id, thread_ts = _get_target(registry, session_key)

        if response_text:
            blocks = build_response_blocks(response_text)
            await slack.post_blocks(channel_id, blocks, "Claude response", thread_ts)
        else:
            await slack.post_text(channel_id, "_Turn completed_", thread_ts)

        return web.Response(text="ok")

    @routes.get("/sessions/{session_id}/replies")
    async def get_replies(request: web.Request) -> web.Response:
        """Pop queued Slack replies for a session."""
        session_id = request.match_info["session_id"]
        queue_dir = config.config_dir / "replies" / session_id
        if not queue_dir.is_dir():
            return web.json_response({"replies": []})
        replies = []
        for f in sorted(queue_dir.iterdir()):
            replies.append(f.read_text())
            f.unlink()
        if not any(queue_dir.iterdir()):
            queue_dir.rmdir()
        return web.json_response({"replies": replies})

    app = web.Application()
    app.router.add_routes(routes)
    return app
