# Claude Slack Bridge

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Slack](https://img.shields.io/badge/Slack-Socket%20Mode-4A154B?logo=slack)](https://api.slack.com/apis/socket-mode)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-Compatible-orange?logo=anthropic)](https://docs.anthropic.com/en/docs/claude-code)

Bridge Claude Code sessions to Slack — chat with Claude from your phone via Slack threads.

## How it works

A daemon runs in the background, connecting Claude Code to Slack via Socket Mode:

```
Slack Thread ←→ Daemon ←→ claude --print (stdin/stdout)
       ↑                         ↑
       └── TUI hooks sync ───────┘
```

**Dual-mode architecture:**
- **PROCESS mode** — Slack drives Claude via `--print` subprocess
- **TUI sync** — hooks sync TUI prompts & responses to Slack thread
- **IDLE mode** — session paused, either side can resume

TUI and Slack can operate on the same session simultaneously — Slack uses `--resume --print` alongside the running TUI.

## Features

- **@mention** in any channel to start a session
- **DM** the bot directly — supports both traditional and Chat mode (Agents & Assistants)
- **Thread replies** continue the conversation (multi-turn)
- **🤔 thinking...** indicator while Claude processes
- **Streaming responses** — live preview updates, final result overwrites progress
- **No "See more"** — responses use plain text to avoid Slack truncation
- **Tool call progress** — `🔧 Bash: git log...` merged into single progress message
- **OPTIONS buttons** — clickable suggestion buttons in Slack
- **👀 reaction** — instant acknowledgment when message received
- **TUI ↔ Slack sync** — TUI prompts and responses sync to Slack thread via hooks
- **Session binding** — `/slack-bridge` command auto-binds TUI session to Slack DM
- **`resume <session-id>`** — bind any TUI session to a Slack thread
- **Session header** — one-click copy `cd /path && claude --resume <UUID>`
- **Markdown → mrkdwn** — proper formatting in Slack
- **Long message splitting** — auto-split at ~3800 chars
- **`yolo off`** — disable auto-approve in thread
- **Max concurrent sessions** — configurable limit prevents resource exhaustion
- **Session cleanup** — stale sessions auto-terminated after timeout
- **Smart cwd inference** — greedy path decoding handles hyphens in directory names

## Install

### As Claude Code Plugin (recommended)

```bash
# Clone
git clone https://github.com/qianheng-aws/claude-slack-bridge.git
cd claude-slack-bridge
python3 -m venv .venv
.venv/bin/pip install -e .

# Register as Claude Code marketplace
claude plugins marketplace add /path/to/claude-slack-bridge
claude plugins install slack-bridge@qianheng-plugins

# Initialize config
.venv/bin/claude-slack-bridge init
# Edit ~/.claude/slack-bridge/.env with your Slack tokens
```

Then in Claude Code TUI:
```
/slack-bridge    → start daemon + bind session to Slack DM
```

### Manual Setup

```bash
git clone https://github.com/qianheng-aws/claude-slack-bridge.git
cd claude-slack-bridge
python3 -m venv .venv
.venv/bin/pip install -e .

# Initialize config
.venv/bin/claude-slack-bridge init

# Edit ~/.claude/slack-bridge/.env:
#   SLACK_BOT_TOKEN=xoxb-...
#   SLACK_APP_TOKEN=xapp-...
```

### Slack App Setup

1. Create app at https://api.slack.com/apps
2. Enable **Socket Mode** (generates `xapp-` token)
3. Add **Bot Token Scopes**: `app_mentions:read`, `channels:history`, `channels:read`, `chat:write`, `im:history`, `im:read`, `reactions:write`
4. **Event Subscriptions** → Subscribe to bot events: `app_mention`, `message.channels`, `message.im`
5. **Interactivity** → Enable (for OPTIONS buttons)
6. Install app to workspace, invite bot to channels

### Enable TUI → Slack Sync

Add hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "/path/to/claude-slack-bridge/bin/claude-slack-bridge-hook user-prompt"}]}],
    "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "/path/to/claude-slack-bridge/bin/claude-slack-bridge-hook stop"}]}]
  }
}
```

The `--print` subprocess sets `CLAUDE_SLACK_BRIDGE_PRINT=1` so hooks auto-skip, preventing loops.

## Usage

### Plugin Commands

| Command | Effect |
|---------|--------|
| `/slack-bridge` | Start daemon + bind current session to Slack DM |
| `/slack-bridge-stop` | Stop daemon |
| `/slack-bridge-status` | Show status and active sessions |
| `/slack-bridge-logs` | View recent daemon logs |

### Slack Commands

| Command | Where | Effect |
|---------|-------|--------|
| `@bot <prompt>` | Channel | New session |
| `<message>` | DM | New session |
| Reply in thread | Thread | Continue session |
| `@bot resume <UUID>` | Channel | Bind TUI session to thread |
| `resume <UUID>` | DM | Bind TUI session to thread |
| `yolo off` | Thread | Disable auto-approve |

### Makefile Shortcuts

```bash
make install   # setup venv and install
make test      # run tests
make start     # start daemon
make stop      # stop daemon
make status    # health check + sessions
make logs      # tail daemon log
```

### TUI ↔ Slack Workflow

```
1. Start TUI:     claude
2. Bind to Slack:  /slack-bridge
3. Chat in TUI  →  prompts & responses sync to Slack thread
4. Reply in Slack → Claude responds in Slack (same session context)
5. Exit TUI      →  continue from Slack, or resume later
```

## Config

`~/.claude/slack-bridge/config.json` (see `config.json.example`):

```json
{
  "daemon_port": 7778,
  "work_dir": "/path/to/default/cwd",
  "claude_args": ["--tools", "Bash,Read,Write,Edit,Glob,Grep"],
  "require_approval": false,
  "auto_approve_tools": ["Read", "Glob", "Grep"],
  "approval_timeout_secs": 300,
  "max_concurrent_sessions": 3,
  "session_archive_after_secs": 3600
}
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full dual-mode state machine design.

## Tests

```bash
make test
# or
.venv/bin/pytest tests/ -q
```

## License

[MIT](LICENSE)
