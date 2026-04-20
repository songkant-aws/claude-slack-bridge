from __future__ import annotations

import logging
import ssl
from pathlib import Path
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger(__name__)

_CA_BUNDLE_PATHS = [
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/certs/ca-certificates.crt",
]


def _make_ssl_context() -> ssl.SSLContext | None:
    """Create an SSL context using the system CA bundle."""
    for path in _CA_BUNDLE_PATHS:
        if Path(path).is_file():
            return ssl.create_default_context(cafile=path)
    return None


class SlackClient:
    """Thin async wrapper around Slack Web API."""

    def __init__(self, bot_token: str) -> None:
        ssl_ctx = _make_ssl_context()
        self._web = AsyncWebClient(token=bot_token, ssl=ssl_ctx)

    @property
    def web(self) -> AsyncWebClient:
        return self._web

    async def post_blocks(
        self,
        channel: str,
        blocks: list[dict],
        text: str = "",
        thread_ts: str | None = None,
    ) -> str:
        """Post a Block Kit message. Returns message ts."""
        kwargs: dict[str, Any] = {
            "channel": channel,
            "blocks": blocks,
            "text": text or "Claude Code Bridge",
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        resp = await self._web.chat_postMessage(**kwargs)
        return resp["ts"]

    async def update_blocks(
        self,
        channel: str,
        ts: str,
        blocks: list[dict],
        text: str = "",
    ) -> None:
        """Update an existing message."""
        await self._web.chat_update(
            channel=channel,
            ts=ts,
            blocks=blocks,
            text=text or "Claude Code Bridge",
        )

    async def post_text(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
    ) -> str:
        """Post a plain text message. Returns message ts."""
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        resp = await self._web.chat_postMessage(**kwargs)
        return resp["ts"]

    async def update_text(self, channel: str, ts: str, text: str) -> None:
        """Update an existing plain-text message."""
        await self._web.chat_update(channel=channel, ts=ts, text=text)

    async def create_channel(self, name: str) -> tuple[str, bool]:
        """Create a channel. Returns (channel_id, created).
        If channel already exists, returns (existing_id, False).
        """
        try:
            resp = await self._web.conversations_create(name=name)
            return resp["channel"]["id"], True
        except Exception as e:
            if "name_taken" in str(e):
                resp = await self._web.conversations_list(types="public_channel")
                for ch in resp.get("channels", []):
                    if ch["name"] == name:
                        return ch["id"], False
            raise

    async def archive_channel(self, channel_id: str) -> None:
        """Archive a channel."""
        try:
            await self._web.conversations_archive(channel=channel_id)
        except Exception:
            logger.warning("Failed to archive channel %s", channel_id, exc_info=True)

    async def auth_test(self) -> dict:
        """Validate token. Returns auth info."""
        resp = await self._web.auth_test()
        return resp.data

    async def add_reaction(self, channel: str, ts: str, emoji: str) -> None:
        """Add a reaction emoji to a message."""
        try:
            await self._web.reactions_add(channel=channel, timestamp=ts, name=emoji)
        except Exception:
            pass  # Ignore duplicates or permission errors

    async def remove_reaction(self, channel: str, ts: str, emoji: str) -> None:
        """Remove a reaction emoji from a message."""
        try:
            await self._web.reactions_remove(channel=channel, timestamp=ts, name=emoji)
        except Exception:
            pass  # Ignore missing reactions or permission errors

    async def set_thread_status(self, channel: str, thread_ts: str, status: str) -> None:
        """Set assistant thread loading status.

        Pass an empty string to clear the status indicator.
        Uses assistant.threads.setStatus API (best-effort).
        """
        try:
            await self._web.api_call(
                "assistant.threads.setStatus",
                params={"channel_id": channel, "thread_ts": thread_ts, "status": status},
            )
        except Exception:
            logger.debug("assistant.threads.setStatus failed", exc_info=True)

    async def set_thread_title(self, channel: str, thread_ts: str, title: str) -> None:
        """Set assistant thread title (best-effort)."""
        try:
            await self._web.api_call(
                "assistant.threads.setTitle",
                params={"channel_id": channel, "thread_ts": thread_ts, "title": title},
            )
        except Exception:
            logger.debug("assistant.threads.setTitle failed", exc_info=True)
