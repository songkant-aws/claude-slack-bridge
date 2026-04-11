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

### Modular daemon (5 files, ~1400 lines total)

The `Daemon` class (daemon.py ~350 lines) is the central orchestrator, composed via mixins:
- `daemon_stream.py` — StreamMixin: progress messages, stream events, JSONL on-demand reading
- `daemon_events.py` — EventsMixin: Socket Mode event handling, interactive buttons
- `daemon_http.py` — HTTP API routes (extracted as `create_http_app(daemon)`)
- `daemon_utils.py` — SeenCache (event dedup), logging setup, path decoding
- `reactions.py` — StatusReactionController: phase-aware emoji reactions with stall detection

It runs:
1. **Slack Socket Mode** client — receives `app_mention`, `message`, `assistant_thread_started`, and `interactive` (button click) events
2. **HTTP API** (aiohttp on port 7778) — receives hook calls from TUI, session bind/list requests, health checks
3. **ProcessPool** — manages `claude --print` subprocesses with stream-json I/O

All three feed into the `SessionManager` which tracks the PROCESS/HOOK/IDLE state machine.

### Message flow

- **Slack -> Claude (PROCESS)**: Socket Mode event -> `_handle_mention`/`_handle_dm` -> `ProcessPool.start()` with `claude --print`
- **Slack -> TUI (HOOK)**: Socket Mode event -> `_handle_thread_reply` -> `tmux send-keys` (bidirectional sync)
- **Claude -> Slack (PROCESS)**: subprocess stdout -> stream-json events -> `_on_stream_event` -> progress message via `chat_update`, finalized on `result`
- **TUI -> Slack (HOOK)**: Claude Code hooks -> `bin/claude-slack-bridge-hook` -> HTTP POST `/hooks/{type}` -> Slack thread
- **TUI Stop -> Slack**: Stop hook triggers on-demand JSONL read for full turn content (Claude Island pattern)

### Key design patterns

- **Single progress message**: All streaming text and tool calls merge into one Slack message (updated via `chat_update`), replaced by the final result. This keeps threads clean.
- **Plain text over blocks**: Final responses use the `text` field (not Block Kit) to avoid Slack's "See more" truncation.
- **Loop prevention**: `CLAUDE_SLACK_BRIDGE_PRINT=1` env var is set on `--print` subprocesses so hooks skip when called from the daemon's own processes.
- **OPTIONS extraction**: Claude can include `[OPTIONS: A | B | C]` in responses, which get parsed and rendered as clickable Slack buttons.

### Two session tracking systems

- `session_manager.py` — The primary system used by `Daemon`. Has the 3-state machine (PROCESS/HOOK/IDLE), `(channel_id, thread_ts)` reverse index, JSON persistence.
- `registry.py` — Legacy system used by `http_api.py` (the hook-only HTTP API). Simpler `SessionMapping` dataclass without mode tracking.

### Session origin tracking

Sessions have an `origin` field ("slack" or "tui") that controls message routing:
- **origin="slack"**: Slack-initiated sessions always use `--print` for follow-up messages
- **origin="tui"**: TUI-initiated sessions try `tmux send-keys` first, fall back to `--print`
- Origin auto-promotes "slack" → "tui" when TUI hooks arrive (e.g., `claude --resume`)
- Origin reverts "tui" → "slack" when tmux send fails (TUI gone)

### Phase-aware reactions (reactions.py)

`StatusReactionController` manages emoji reactions showing processing phase:
queued(eyes) → thinking(thinking_face) → coding(technologist) → browsing(globe) → tool(wrench) → done(lobster) / error(rotating_light). 700ms debounce, stall detection at 15s/45s. For TUI sessions, controller stored in `_reaction_controllers` dict, updated by PostToolUse/Stop hooks.

### Hook pipeline (plugins/slack-bridge/hooks/hooks.json)

Hooks are registered via the plugin's `hooks.json` (10 event types). The hook script `bin/claude-slack-bridge-hook` uses **stdlib urllib only** (no aiohttp) to POST to the daemon. Key hooks:
- **PreToolUse** — fire-and-forget, auto-approves for TUI sessions (no Slack blocking)
- **PermissionRequest** — blocks waiting for Slack approval buttons (Approve/Trust/YOLO/Reject). Replaces TUI's built-in approval dialog. Falls through to TUI prompt on timeout.
- **PostToolUse** — fire-and-forget, updates tool status in progress message + phase-aware reaction
- **UserPromptSubmit** — fire-and-forget, syncs TUI-typed prompts to Slack (Slack-forwarded prompts filtered)
- **Stop** — reads JSONL file for full turn content, posts final response to Slack
- **SessionStart/SessionEnd** — track TUI session lifecycle, auto-promote IDLE→HOOK

If the daemon is unreachable, all hooks return 0 to never block Claude Code.

### Slack formatting (slack_formatter.py)

Full Markdown → Slack mrkdwn pipeline: ANSI stripping, bold, headings, links, strikethrough, horizontal rules, markdown tables → vertical bullet format, mermaid diagrams → text arrows, `<thinking>` tag extraction. Messages split via `split_message()` with continuation markers. Approval buttons: Approve / Trust Session / YOLO / Reject.

### Config and runtime paths

- Config dir: `~/.claude/slack-bridge/` (`.env` for tokens, `config.json` for settings)
- State: `sessions.json`, `daemon.pid`, `daemon.log` in config dir
- Tokens: `SLACK_BOT_TOKEN` (xoxb-) and `SLACK_APP_TOKEN` (xapp-) in `.env`

## Testing

Tests use `pytest-asyncio` (auto mode) and `pytest-aiohttp` for HTTP endpoint testing. Slack API calls are mocked with `AsyncMock`. The `conftest.py` provides `tmp_config_dir`, `bridge_config`, `bridge_registry`, and `bridge_approval` fixtures.

### Context injection

The daemon automatically reads this `CLAUDE.md` file and injects it into the `--system-prompt` of every `--print` subprocess. This gives Slack-originated sessions full awareness of the plugin's architecture. The file is searched at: project root (editable install), `~/.claude/plugins/slack-bridge/`, and package directory.

## Plugin structure

The repo doubles as a Claude Code marketplace plugin. Slash commands are namespaced under `slack-bridge:` and defined as markdown files in `plugins/slack-bridge/commands/`:
- `/slack-bridge:sync-on` — start daemon + bind session to Slack DM
- `/slack-bridge:start-daemon` — start daemon only
- `/slack-bridge:stop-daemon` — stop daemon
- `/slack-bridge:status` — show status and active sessions
- `/slack-bridge:logs` — view recent daemon logs
- `/slack-bridge:sync-off` — mute TUI→Slack sync for current session
