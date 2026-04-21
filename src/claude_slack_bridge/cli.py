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


def _read_env_tokens(env_file: Path) -> tuple[str, str]:
    """Return (app_token, bot_token) already on disk, empty strings if missing."""
    if not env_file.is_file():
        return "", ""
    app_tok = ""
    bot_tok = ""
    for line in env_file.read_text().splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "SLACK_APP_TOKEN":
            app_tok = v
        elif k == "SLACK_BOT_TOKEN":
            bot_tok = v
    return app_tok, bot_tok


@click.group()
def main() -> None:
    """Claude Slack Bridge — bridge Claude Code sessions to Slack."""
    pass


@main.command()
def init() -> None:
    """Interactive setup: tokens, config, PATH symlink."""
    config_dir = Path.home() / ".claude" / "slack-bridge"
    config_dir.mkdir(parents=True, exist_ok=True)

    env_file = config_dir / ".env"
    existing_app, existing_bot = _read_env_tokens(env_file)

    hint = "(press enter to keep existing)" if existing_app or existing_bot else ""
    prompt_app = f"Slack App Token (xapp-...) {hint}".rstrip()
    prompt_bot = f"Slack Bot Token (xoxb-...) {hint}".rstrip()
    app_token = click.prompt(prompt_app, default="", show_default=False) or existing_app
    bot_token = click.prompt(prompt_bot, default="", show_default=False) or existing_bot

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

    if app_token != existing_app or bot_token != existing_bot:
        env_file.write_text(f"SLACK_APP_TOKEN={app_token}\nSLACK_BOT_TOKEN={bot_token}\n")
        click.echo(f"Tokens saved to {env_file}")
    else:
        click.echo(f"Tokens unchanged ({env_file})")

    _refresh_defaults(config_dir)

    click.echo(
        "\nSetup complete! Install the Claude Code plugin to wire up hooks:\n"
        "  claude plugins install slack-bridge@qianheng-plugins\n"
        "Then run 'claude-slack-bridge start' to launch the daemon."
    )


def _refresh_defaults(config_dir: Path) -> None:
    """Non-interactive side of `init`: config.json, launcher symlink, legacy cleanup.

    Safe to re-run on every upgrade — all operations are idempotent.
    """
    config_file = config_dir / "config.json"
    if not config_file.exists():
        # Minimal set of fields users are most likely to tweak; the rest
        # fall back to defaults defined in BridgeConfig.
        default_cfg = {
            "auto_approve_tools": ["Read", "Glob", "Grep"],
            "log_level": "INFO",
            "claude_args": [],
        }
        config_file.write_text(json.dumps(default_cfg, indent=2))
        click.echo(f"Config saved to {config_file}")

    _install_launcher()
    _remove_legacy_permission_hook()


# Older versions of init injected a `PermissionRequest` block into
# ~/.claude/settings.json (marker below) as a workaround for a
# misread CC behavior — plugin-scoped PermissionRequest hooks do fire,
# we just had the matcher wrong. This cleanup removes the legacy
# block on upgrade so users don't end up with duplicate handlers.
_LEGACY_PERMISSION_HOOK_MARKER = "claude-slack-bridge:PermissionRequest"


def _remove_legacy_permission_hook() -> None:
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.is_file():
        return
    try:
        settings = json.loads(settings_path.read_text())
    except json.JSONDecodeError:
        return
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    entries = hooks.get("PermissionRequest")
    if not isinstance(entries, list):
        return
    kept = [e for e in entries if e.get("_marker") != _LEGACY_PERMISSION_HOOK_MARKER]
    if len(kept) == len(entries):
        return
    if kept:
        hooks["PermissionRequest"] = kept
    else:
        hooks.pop("PermissionRequest", None)
        if not hooks:
            settings.pop("hooks", None)
    settings_path.write_text(json.dumps(settings, indent=2))
    click.echo(f"Removed legacy PermissionRequest workaround from {settings_path}")


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
    unit = _find_systemd_unit()
    if unit:
        _systemctl_invoke(unit, "stop")
        return
    _stop_daemon()


_SYSTEMD_UNIT_NAME = "claude-slack-bridge.service"


def _find_systemd_unit() -> tuple[str, bool] | None:
    """Return (unit, is_user) if the systemd unit is enabled, else None.

    is_user True → `systemctl --user`; False → system-level `systemctl` (needs sudo).
    """
    system = Path(f"/etc/systemd/system/{_SYSTEMD_UNIT_NAME}")
    user = Path.home() / ".config" / "systemd" / "user" / _SYSTEMD_UNIT_NAME
    if user.is_file():
        return (_SYSTEMD_UNIT_NAME, True)
    if system.is_file():
        return (_SYSTEMD_UNIT_NAME, False)
    return None


def _systemctl_invoke(unit_info: tuple[str, bool], verb: str) -> None:
    import subprocess
    unit, is_user = unit_info
    cmd = ["systemctl"]
    if is_user:
        cmd.append("--user")
    else:
        cmd.insert(0, "sudo")
    cmd += [verb, unit]
    click.echo(f"Delegating to systemd: {' '.join(cmd)}")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        click.echo(f"systemctl {verb} exited with status {rc}", err=True)
        raise SystemExit(rc)


def _stop_daemon() -> bool:
    """Send SIGTERM to a running daemon and wait briefly for it to exit.

    Returns True if the port is free when we're done.
    """
    import signal
    import time as _time

    cfg = load_config()
    pid_file = cfg.config_dir / "daemon.pid"
    pid: int | None = None
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text().strip())
        except ValueError:
            pid_file.unlink(missing_ok=True)

    # Fallback: no valid pid file but port still bound — find owner by port.
    if pid is None:
        pid = _find_pid_by_port(cfg.daemon_port)
        if pid is None:
            click.echo("Daemon not running.")
            return True
        click.echo(f"No PID file, but port {cfg.daemon_port} is held by PID {pid}")

    try:
        os.kill(pid, signal.SIGTERM)
        click.echo(f"Sent SIGTERM to daemon (PID {pid})")
    except ProcessLookupError:
        click.echo("Daemon not running (stale reference)")
        pid_file.unlink(missing_ok=True)
        return True
    for _ in range(50):
        try:
            os.kill(pid, 0)
            _time.sleep(0.1)
        except ProcessLookupError:
            return True
    click.echo(f"Daemon (PID {pid}) did not exit within 5s.", err=True)
    return False


def _find_pid_by_port(port: int) -> int | None:
    """Best-effort: find the PID listening on the given localhost TCP port."""
    import re
    import subprocess
    try:
        out = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=3, check=False,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if f":{port} " not in line:
            continue
        m = re.search(r"pid=(\d+)", line)
        if m:
            return int(m.group(1))
    return None


@main.command()
@click.option("-d", "--daemonize", is_flag=True, help="Run in background")
def restart(daemonize: bool) -> None:
    """Stop the daemon (if running) and start a fresh one."""
    unit = _find_systemd_unit()
    if unit:
        _systemctl_invoke(unit, "restart")
        return
    _stop_daemon()
    cfg = load_config()
    if not cfg.slack_app_token or not cfg.slack_bot_token:
        click.echo("Slack tokens not configured. Run 'claude-slack-bridge init' first.", err=True)
        raise SystemExit(1)
    if daemonize:
        _daemonize()
    from claude_slack_bridge.daemon import Daemon
    asyncio.run(Daemon(cfg).start())


@main.command()
@click.option(
    "--skip-plugin",
    is_flag=True,
    help="Skip `claude plugins update` (use when the CLI isn't on PATH).",
)
def update(skip_plugin: bool) -> None:
    """Sync daemon code, plugin cache, and launcher, then restart the daemon.

    Run this after `git pull`. Collapses the four-step upgrade into one
    command so you don't have to remember pip/plugins update/init/restart
    ordering. You'll still need to restart your TUI afterward so it
    re-reads the refreshed plugin cache.
    """
    import shutil
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent.parent
    pip = Path(sys.executable).with_name("pip")

    click.echo("→ [1/4] Installing Python package (editable)")
    rc = subprocess.run(
        [str(pip), "install", "-e", str(repo_root)],
        capture_output=True, text=True,
    )
    if rc.returncode != 0:
        click.echo(rc.stderr, err=True)
        click.echo("pip install failed — aborting.", err=True)
        raise SystemExit(rc.returncode)

    click.echo("→ [2/4] Refreshing Claude Code plugin cache")
    if skip_plugin:
        click.echo("  (skipped)")
    elif not shutil.which("claude"):
        click.echo(
            "  `claude` not on PATH — skipping plugin cache refresh. "
            "Run `claude plugins update slack-bridge@qianheng-plugins` manually."
        )
    else:
        rc = subprocess.run(
            ["claude", "plugins", "update", "slack-bridge@qianheng-plugins"],
        )
        if rc.returncode != 0:
            click.echo(
                "  `claude plugins update` exited with non-zero status — continuing anyway.",
                err=True,
            )

    click.echo("→ [3/4] Refreshing launcher and config defaults")
    config_dir = Path.home() / ".claude" / "slack-bridge"
    config_dir.mkdir(parents=True, exist_ok=True)
    _refresh_defaults(config_dir)

    click.echo("→ [4/4] Restarting daemon")
    unit = _find_systemd_unit()
    if unit:
        _systemctl_invoke(unit, "restart")
    else:
        _stop_daemon()
        click.echo(
            "  No systemd unit installed — start the daemon manually with "
            "`claude-slack-bridge start -d` when ready."
        )

    click.echo("\n✅ Update complete. Restart your Claude Code TUI to pick up plugin changes.")


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
