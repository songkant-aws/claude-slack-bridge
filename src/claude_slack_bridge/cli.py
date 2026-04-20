from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import click

from claude_slack_bridge.config import BridgeConfig, load_config


_RC_MARKER = "# claude-slack-bridge"
_LOCAL_BIN = Path.home() / ".local" / "bin"


@click.group()
def main() -> None:
    """Claude Slack Bridge — bridge Claude Code sessions to Slack."""
    pass


@main.command()
def init() -> None:
    """Interactive setup: tokens, config, PATH symlink."""
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

    _install_launcher()
    _install_permission_request_hook()

    click.echo(
        "\nSetup complete! Install the Claude Code plugin to wire up hooks:\n"
        "  claude plugins install slack-bridge@qianheng-plugins\n"
        "Then run 'claude-slack-bridge start' to launch the daemon."
    )


# Marker for the PermissionRequest hook we inject into settings.json.
# The plugin's hooks.json ships PermissionRequest too, but upstream CC
# silently drops plugin-scoped hooks (see issue #14 / anthropics/claude-code#27398),
# so we register it directly in settings.json as well. Safe to remove
# once that upstream bug is fixed.
_PERMISSION_HOOK_MARKER = "claude-slack-bridge:PermissionRequest"


def _find_plugin_hook_script() -> Path | None:
    """Locate the plugin's hook binary in the Claude Code plugin cache."""
    cache = Path.home() / ".claude" / "plugins" / "cache" / "qianheng-plugins" / "slack-bridge"
    if not cache.is_dir():
        return None
    versions = [v for v in cache.iterdir() if v.is_dir()]
    if not versions:
        return None
    latest = max(versions, key=lambda v: v.stat().st_mtime)
    script = latest / "bin" / "claude-slack-bridge-hook"
    return script if script.is_file() else None


def _install_permission_request_hook() -> None:
    """Register PermissionRequest in ~/.claude/settings.json.

    Workaround for upstream bug: plugin-scoped PermissionRequest hooks
    do not fire (see tracker issue #14). Registering directly in
    settings.json bypasses the --setting-sources exclusion.
    """
    script = _find_plugin_hook_script()
    if not script:
        click.echo(
            "Claude Code plugin not installed yet — skipping PermissionRequest registration. "
            "Run `claude plugins install slack-bridge@qianheng-plugins` and re-run init."
        )
        return

    settings_path = Path.home() / ".claude" / "settings.json"
    settings: dict = {}
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            click.echo(f"Cannot parse {settings_path}; skipping PermissionRequest registration.")
            return

    hooks = settings.setdefault("hooks", {})
    entries = hooks.get("PermissionRequest", [])

    # Idempotent: replace any existing marker-tagged block
    entries = [e for e in entries if e.get("_marker") != _PERMISSION_HOOK_MARKER]
    entries.append({
        "_marker": _PERMISSION_HOOK_MARKER,
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": f"{script} PermissionRequest",
            "timeout": 86410000,
        }],
    })
    hooks["PermissionRequest"] = entries

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2))
    click.echo(f"Registered PermissionRequest hook in {settings_path}")


def _install_launcher() -> None:
    """Symlink the venv entry point into ~/.local/bin and ensure PATH."""
    # sys.argv[0] resolves to the entry point script pip placed in venv/bin.
    entry = Path(sys.argv[0]).resolve()
    if not entry.is_file():
        click.echo(f"Could not locate claude-slack-bridge launcher (looked at {entry}). Skipping symlink.")
        return

    _LOCAL_BIN.mkdir(parents=True, exist_ok=True)
    link = _LOCAL_BIN / "claude-slack-bridge"
    current = link.resolve() if link.is_symlink() or link.exists() else None
    if link.is_symlink() or link.exists():
        if current == entry:
            click.echo(f"Launcher already linked: {link} → {entry}")
        else:
            link.unlink()
            link.symlink_to(entry)
            click.echo(f"Updated launcher: {link} → {entry}")
    else:
        link.symlink_to(entry)
        click.echo(f"Linked launcher: {link} → {entry}")

    _update_shell_rc()


def _update_shell_rc() -> None:
    """Append `export PATH="$HOME/.local/bin:$PATH"` to common rc files (idempotent)."""
    path_line = 'export PATH="$HOME/.local/bin:$PATH"'
    block = f"\n{_RC_MARKER}\n{path_line}\n"

    for rc_name in (".bashrc", ".zshrc"):
        rc = Path.home() / rc_name
        if not rc.is_file():
            continue
        existing = rc.read_text()
        if _RC_MARKER in existing:
            continue
        with rc.open("a") as fh:
            fh.write(block)
        click.echo(f"Added ~/.local/bin to PATH in {rc}")

    # If the user's current shell hasn't sourced the rc yet, warn them.
    if str(_LOCAL_BIN) not in os.environ.get("PATH", "").split(":"):
        click.echo(
            f"Note: ~/.local/bin is not on your current PATH. Restart your shell "
            f"or run: export PATH=\"{_LOCAL_BIN}:$PATH\""
        )


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


if __name__ == "__main__":
    main()
