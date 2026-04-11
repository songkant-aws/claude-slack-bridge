from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import click

from claude_slack_bridge.config import BridgeConfig, load_config


@click.group()
def main() -> None:
    """Claude Slack Bridge — bridge Claude Code sessions to Slack."""
    pass


@main.command()
def init() -> None:
    """Interactive setup: configure tokens and register hooks."""
    config_dir = Path.home() / ".claude" / "slack-bridge"
    config_dir.mkdir(parents=True, exist_ok=True)

    app_token = click.prompt("Slack App Token (xapp-...)", default="", show_default=False)
    bot_token = click.prompt("Slack Bot Token (xoxb-...)", default="", show_default=False)

    if app_token and bot_token:
        click.echo("Validating tokens...")
        import asyncio as _aio
        from claude_slack_bridge.slack_client import SlackClient

        async def _validate() -> bool:
            client = SlackClient(bot_token)
            try:
                info = await client.auth_test()
                click.echo(f"Authenticated as: {info.get('user', 'unknown')}")
                return True
            except Exception as e:
                click.echo(f"Token validation failed: {e}", err=True)
                return False

        if not _aio.run(_validate()):
            if not click.confirm("Continue with invalid tokens?"):
                raise SystemExit(1)

    env_file = config_dir / ".env"
    env_file.write_text(f"SLACK_APP_TOKEN={app_token}\nSLACK_BOT_TOKEN={bot_token}\n")
    click.echo(f"Tokens saved to {env_file}")

    config_file = config_dir / "config.json"
    if not config_file.exists():
        default_cfg = {
            "daemon_port": 7778,
            "default_channel": "claude-code",
            "approval_timeout_secs": 300,
            "auto_approve_tools": ["Read", "Glob", "Grep"],
            "require_approval": True,
            "truncate_chars": 3000,
            "session_archive_after_secs": 86400,
            "log_level": "INFO",
        }
        config_file.write_text(json.dumps(default_cfg, indent=2))
        click.echo(f"Config saved to {config_file}")

    _register_hooks(config_dir)
    click.echo("Setup complete! Run 'claude-slack-bridge start' to launch the daemon.")


def _register_hooks(config_dir: Path) -> None:
    """Register hooks in ~/.claude/settings.json.

    Skips registration if the slack-bridge plugin is installed (its
    hooks.json already provides all necessary hooks).
    """
    # Check if plugin hooks already handle this
    plugin_hooks = config_dir.parent / "plugins" / "cache" / "slack-bridge"
    if plugin_hooks.exists():
        click.echo("Hooks already provided by slack-bridge plugin — skipping settings.json registration.")
        return

    settings_path = Path.home() / ".claude" / "settings.json"
    settings: dict = {}
    if settings_path.is_file():
        settings = json.loads(settings_path.read_text())

    hooks_config = {
        "PostToolUse": [{
            "matcher": "",
            "hooks": [{
                "type": "command",
                "command": "claude-slack-bridge hook post-tool-use",
            }],
        }],
        "UserPromptSubmit": [{
            "matcher": "",
            "hooks": [{
                "type": "command",
                "command": "claude-slack-bridge hook user-prompt",
            }],
        }],
        "Stop": [{
            "matcher": "",
            "hooks": [{
                "type": "command",
                "command": "claude-slack-bridge hook stop",
            }],
        }],
    }

    existing_hooks = settings.get("hooks", {})
    existing_hooks.update(hooks_config)
    settings["hooks"] = existing_hooks

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2))
    click.echo(f"Hooks registered in {settings_path}")


@main.command()
@click.option("-d", "--daemonize", is_flag=True, help="Run in background")
def start(daemonize: bool) -> None:
    """Start the bridge daemon."""
    cfg = load_config()
    if not cfg.slack_app_token or not cfg.slack_bot_token:
        click.echo("Slack tokens not configured. Run 'claude-slack-bridge init' first.", err=True)
        raise SystemExit(1)

    if daemonize:
        _daemonize()

    from claude_slack_bridge.daemon import Daemon

    daemon = Daemon(cfg)
    asyncio.run(daemon.start())


def _daemonize() -> None:
    """Fork into background. Unix only (Linux/macOS)."""
    if sys.platform == "win32":
        click.echo("Background mode not supported on Windows. Run in foreground.", err=True)
        raise SystemExit(1)
    if os.fork() > 0:
        raise SystemExit(0)
    os.setsid()
    if os.fork() > 0:
        raise SystemExit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)


@main.command()
def stop() -> None:
    """Stop the bridge daemon."""
    import signal
    import urllib.request
    import urllib.error

    cfg = load_config()
    pid_file = cfg.config_dir / "daemon.pid"
    if pid_file.is_file():
        pid = int(pid_file.read_text().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            click.echo(f"Sent SIGTERM to daemon (PID {pid})")
        except ProcessLookupError:
            click.echo("Daemon not running (stale PID file)")
            pid_file.unlink()
    else:
        click.echo("No PID file found. Daemon may not be running.")


@main.command()
def status() -> None:
    """Show daemon status and active sessions."""
    import urllib.request
    import urllib.error

    cfg = load_config()
    try:
        url = f"http://127.0.0.1:{cfg.daemon_port}/sessions"
        with urllib.request.urlopen(url, timeout=3) as resp:
            sessions = json.loads(resp.read())
        click.echo(f"Daemon running on port {cfg.daemon_port}")
        if sessions:
            click.echo(f"\nActive sessions ({len(sessions)}):")
            for s in sessions:
                mode = "channel" if s.get("is_dedicated_channel") else "thread"
                click.echo(f"  {s['session_id']} ({s['session_name']}) [{mode}]")
        else:
            click.echo("No active sessions.")
    except (urllib.error.URLError, OSError):
        click.echo("Daemon is not running.")


@main.group()
def hook() -> None:
    """Hook entry points (called by Claude Code, not users)."""
    pass


@hook.command("pre-tool-use")
def hook_pre_tool_use() -> None:
    from claude_slack_bridge.hooks import run_hook
    sys.exit(run_hook("pre-tool-use"))


@hook.command("post-tool-use")
def hook_post_tool_use() -> None:
    from claude_slack_bridge.hooks import run_hook
    sys.exit(run_hook("post-tool-use"))


@hook.command("user-prompt")
def hook_user_prompt() -> None:
    from claude_slack_bridge.hooks import run_hook
    sys.exit(run_hook("user-prompt"))


@hook.command("stop")
def hook_stop() -> None:
    from claude_slack_bridge.hooks import run_hook
    sys.exit(run_hook("stop"))


if __name__ == "__main__":
    main()
