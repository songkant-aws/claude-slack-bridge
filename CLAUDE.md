# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Claude Slack Bridge connects Claude Code sessions to Slack via a background daemon. Users can chat with Claude from Slack threads (e.g., on their phone) and seamlessly hand off between the TUI and Slack. The daemon manages a 3-state session lifecycle: PROCESS (daemon runs `claude --print`), HOOK (TUI active, hooks sync to Slack), and IDLE (neither active, either side can resume).

## Commands

```bash
make install        # Create venv, install package in editable mode
make test           # Run all tests: .venv/bin/python -m pytest tests/ -q
make start          # Start daemon (background, port 7778)
make stop           # Stop daemon via PID file
make status         # Health check + list active sessions
make logs           # tail -50 daemon log

# Run a single test
.venv/bin/python -m pytest tests/test_slack_formatter.py -q
.venv/bin/python -m pytest tests/test_daemon.py::test_daemon_init -q

# Dev dependencies
.venv/bin/pip install -e ".[dev]"
```

## Architecture

### Dual-mode daemon (daemon.py ~760 lines)

The `Daemon` class is the central orchestrator. It runs:
1. **Slack Socket Mode** client — receives `app_mention`, `message`, and `interactive` (button click) events
2. **HTTP API** (aiohttp on port 7778) — receives hook calls from TUI, session bind/list requests, health checks
3. **ProcessPool** — manages `claude --print` subprocesses with stream-json I/O

All three feed into the `SessionManager` which tracks the PROCESS/HOOK/IDLE state machine.

### Message flow

- **Slack -> Claude**: Socket Mode event -> `_handle_mention`/`_handle_dm`/`_handle_thread_reply` -> `ProcessPool.start()` or `ClaudeProcess.send_message()` (writes to subprocess stdin)
- **Claude -> Slack**: subprocess stdout -> `_read_stdout` -> `parse_line` (stream_parser.py) -> `_on_stream_event` -> progress message updates via `chat_update`, finalized on `result` event
- **TUI -> Slack**: Claude Code hooks -> `bin/claude-slack-bridge-hook` -> HTTP POST to daemon `/hooks/{type}` -> Slack thread post

### Key design patterns

- **Single progress message**: All streaming text and tool calls merge into one Slack message (updated via `chat_update`), replaced by the final result. This keeps threads clean.
- **Plain text over blocks**: Final responses use the `text` field (not Block Kit) to avoid Slack's "See more" truncation.
- **Loop prevention**: `CLAUDE_SLACK_BRIDGE_PRINT=1` env var is set on `--print` subprocesses so hooks skip when called from the daemon's own processes.
- **OPTIONS extraction**: Claude can include `[OPTIONS: A | B | C]` in responses, which get parsed and rendered as clickable Slack buttons.

### Two session tracking systems

- `session_manager.py` — The primary system used by `Daemon`. Has the 3-state machine (PROCESS/HOOK/IDLE), `(channel_id, thread_ts)` reverse index, JSON persistence.
- `registry.py` — Legacy system used by `http_api.py` (the hook-only HTTP API). Simpler `SessionMapping` dataclass without mode tracking.

The `http_api.py` module is the original hook-only API that predates the dual-mode daemon. The daemon's `_create_http_app()` implements its own superset of these endpoints inline.

### Hook pipeline (hooks.py)

Hooks are synchronous CLI commands invoked by Claude Code. They use **stdlib urllib only** (no aiohttp) to POST to the daemon. `pre-tool-use` blocks waiting for approval; all others fire-and-forget. If the daemon is unreachable, hooks return 0 to never block Claude Code.

### Slack formatting (slack_formatter.py)

Converts Markdown to Slack mrkdwn (`**bold**` -> `*bold*`, headers, links). All `build_*_blocks()` functions return Block Kit JSON. Text is truncated at ~2500-3800 chars; long messages are split at newline boundaries.

### Config and runtime paths

- Config dir: `~/.claude/slack-bridge/` (`.env` for tokens, `config.json` for settings)
- State: `sessions.json`, `daemon.pid`, `daemon.log` in config dir
- Tokens: `SLACK_BOT_TOKEN` (xoxb-) and `SLACK_APP_TOKEN` (xapp-) in `.env`

## Testing

Tests use `pytest-asyncio` (auto mode) and `pytest-aiohttp` for HTTP endpoint testing. Slack API calls are mocked with `AsyncMock`. The `conftest.py` provides `tmp_config_dir`, `bridge_config`, `bridge_registry`, and `bridge_approval` fixtures.

## Plugin structure

The repo doubles as a Claude Code marketplace plugin. The `/slack-bridge` command (and status/stop/logs variants) are defined as markdown files in `commands/` and `plugins/slack-bridge/commands/`. These contain shell scripts that the TUI executes directly.
