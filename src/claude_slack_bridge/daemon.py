from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
from pathlib import Path

from aiohttp import web
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from claude_slack_bridge.approval import ApprovalManager
from claude_slack_bridge.config import BridgeConfig
from claude_slack_bridge.http_api import create_app
from claude_slack_bridge.registry import SessionRegistry
from claude_slack_bridge.slack_client import SlackClient

logger = logging.getLogger("claude_slack_bridge")


def _setup_logging(config: BridgeConfig) -> None:
    log_dir = config.config_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "daemon.log"

    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))

    root = logging.getLogger("claude_slack_bridge")
    root.setLevel(getattr(logging, config.log_level, logging.INFO))
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())


class Daemon:
    def __init__(self, config: BridgeConfig) -> None:
        self._config = config
        self._registry = SessionRegistry(
            storage_path=config.config_dir / "sessions.json"
        )
        self._approval_mgr = ApprovalManager()
        self._slack: SlackClient | None = None
        self._socket_client: SocketModeClient | None = None
        self._runner: web.AppRunner | None = None
        self._cleanup_task: asyncio.Task | None = None

    async def _handle_interactive_action(
        self,
        action_id: str,
        value: str,
        channel: str,
        msg_ts: str,
    ) -> None:
        """Handle a Slack interactive button click."""
        request_id = value
        if action_id == "approve_tool":
            self._approval_mgr.resolve(request_id, "approved")
        elif action_id == "reject_tool":
            self._approval_mgr.resolve(request_id, "rejected")
        else:
            logger.warning("Unknown action_id: %s", action_id)

    async def _on_socket_event(
        self,
        client: SocketModeClient,
        req: SocketModeRequest,
    ) -> None:
        """Handle all Socket Mode events."""
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        if req.type == "interactive":
            payload = req.payload or {}
            actions = payload.get("actions", [])
            channel_info = payload.get("channel", {})
            channel_id = channel_info.get("id", "")
            message = payload.get("message", {})
            msg_ts = message.get("ts", "")

            for action in actions:
                await self._handle_interactive_action(
                    action_id=action.get("action_id", ""),
                    value=action.get("value", ""),
                    channel=channel_id,
                    msg_ts=msg_ts,
                )

    async def start(self) -> None:
        """Start the daemon: HTTP server + Slack Socket Mode."""
        _setup_logging(self._config)
        logger.info("Starting Claude Slack Bridge daemon on port %d", self._config.daemon_port)

        self._slack = SlackClient(self._config.slack_bot_token)

        app = create_app(
            self._config, self._registry, self._approval_mgr, self._slack
        )
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._config.daemon_port)
        try:
            await site.start()
        except OSError as e:
            logger.error("Port %d in use: %s", self._config.daemon_port, e)
            raise SystemExit(1) from e

        logger.info("HTTP API listening on 127.0.0.1:%d", self._config.daemon_port)

        if self._config.slack_app_token and self._config.slack_bot_token:
            self._socket_client = SocketModeClient(
                app_token=self._config.slack_app_token,
                web_client=self._slack.web,
            )
            self._socket_client.socket_mode_request_listeners.append(
                self._on_socket_event
            )
            await self._socket_client.connect()
            logger.info("Slack Socket Mode connected")
        else:
            logger.warning("Slack tokens not configured — running in HTTP-only mode")

        import os
        pid_file = self._config.config_dir / "daemon.pid"
        pid_file.write_text(str(os.getpid()))

        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()

        logger.info("Shutting down...")
        await self.stop()

    async def stop(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self._socket_client:
            await self._socket_client.close()
        if self._runner:
            await self._runner.cleanup()
        pid_file = self._config.config_dir / "daemon.pid"
        pid_file.unlink(missing_ok=True)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            archived = self._registry.cleanup(
                max_idle_secs=self._config.session_archive_after_secs
            )
            if archived:
                logger.info("Archived %d idle sessions: %s", len(archived), archived)
