# Claude Slack Bridge

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Slack](https://img.shields.io/badge/Slack-Socket%20Mode-4A154B?logo=slack)](https://api.slack.com/apis/socket-mode)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-Plugin-orange?logo=anthropic)](https://docs.anthropic.com/en/docs/claude-code)

**Remote-control Claude Code from Slack.** Start sessions from your phone, sync your TUI to a thread, and let your team watch along — like [Remote Control](https://code.claude.com/docs/en/remote-control) or [OpenClaw](https://github.com/openclaw/openclaw), but through the tool your team already lives in.

Works with Bedrock, API keys, and any Claude Code setup. No claude.ai subscription required.

English | [中文](README.zh.md)

<p align="center">
  <img src="docs/demo.gif" alt="Claude Slack Bridge Demo" width="600">
</p>

## What can you do with it?

**Commute coding** — DM the bot from your phone: "refactor the auth middleware to use JWT". Claude works on your cloud dev machine. By the time you arrive, the work is done — `claude --resume` to review and iterate.

**Meeting multitasking** — Kick off a long task in Slack ("migrate the database schema and update all tests"), check progress between agenda items. Claude keeps working while you're in the meeting.

**Team visibility** — @mention the bot in a project channel. The whole team sees Claude's work in the thread — great for demos, pair debugging, or keeping teammates in the loop.

**On-call incident response** — Get paged at 2am? DM the bot from bed: "check the error logs in /var/log/app and find the root cause". Triage from your phone before deciding whether to get up.

**Long-running tasks** — Start a large refactor from Slack, go about your day. Slack notifications tell you when Claude needs input or finishes. No terminal session to keep alive.

## How it works

### Slack-first: remote control from your phone

DM or @mention the bot, and a full Claude Code session starts on your machine — reading files, editing code, running tests. All from Slack.

```
1. @bot or DM     →  Claude Code session starts on your machine
2. Chat in thread →  Claude reads, edits, runs tests — streams results back
3. Keep chatting  →  multi-turn conversation, full tool access
```

When you're back at your computer, every session header includes a one-liner to pick up in TUI:

```bash
cd /your/project && claude --resume <session-id>
```

### TUI-first: sync to Slack

Working at your desk? Use TUI as usual. Whenever you want Slack as a mirror — run `/slack-bridge:sync-on`. From that point on, everything syncs bidirectionally.

```
1. Start TUI:          claude
2. Work as usual       (sync-on can happen anytime)
3. /slack-bridge:sync-on → session binds to a Slack DM thread
4. Leave for lunch     →  pull out your phone, reply in the Slack thread
5. Message goes to TUI →  tmux send-keys delivers it directly into your session
6. Claude responds     →  response syncs back to Slack automatically
7. Back at desk        →  keep working in TUI, full context preserved
```

## How is this different?

| | Remote Control | OpenClaw | **Claude Slack Bridge** |
|---|---|---|---|
| **Client** | claude.ai / Claude app | WhatsApp, Telegram, Slack, etc. | **Slack** |
| **Auth** | claude.ai Pro/Max/Team/Enterprise | Bring your own LLM key | **Any Claude Code setup (Bedrock, API key)** |
| **Team visibility** | Private | Single-user | **Shared in channels — team can follow along** |
| **Integration** | Standalone UI | Multi-channel gateway | **Native Slack (threads, @mentions, notifications)** |
| **Session handoff** | Web ↔ TUI | N/A | **Slack ↔ TUI via `claude --resume`** |

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/qianheng-aws/claude-slack-bridge.git
cd claude-slack-bridge && python3 -m venv .venv && .venv/bin/pip install -e .

# 2. Register as Claude Code plugin
claude plugins marketplace add /path/to/claude-slack-bridge
claude plugins install slack-bridge@qianheng-plugins

# 3. Initialize and add your Slack tokens
.venv/bin/claude-slack-bridge init
# Edit ~/.claude/slack-bridge/.env with SLACK_BOT_TOKEN and SLACK_APP_TOKEN
```

Then in Claude Code TUI:
```
/slack-bridge:sync-on
```

<details>
<summary><b>Slack App Setup</b></summary>

1. Create app at https://api.slack.com/apps
2. Enable **Socket Mode** (generates `xapp-` token)
3. Add **Bot Token Scopes**: `app_mentions:read`, `channels:history`, `channels:read`, `chat:write`, `im:history`, `im:read`, `reactions:write`
4. **Event Subscriptions** → Subscribe to bot events: `app_mention`, `message.channels`, `message.im`
5. **Interactivity** → Enable (for OPTIONS buttons)
6. **Assistant** → Enable `assistant_view` in app manifest with `assistant:write` scope
7. **Event Subscriptions** → Also subscribe to: `assistant_thread_started`, `assistant_thread_context_changed`
8. Install app to workspace, invite bot to channels

</details>

<details>
<summary><b>Manual Setup (without plugin)</b></summary>

```bash
git clone https://github.com/qianheng-aws/claude-slack-bridge.git
cd claude-slack-bridge && python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/claude-slack-bridge init
# Edit ~/.claude/slack-bridge/.env:
#   SLACK_BOT_TOKEN=xoxb-...
#   SLACK_APP_TOKEN=xapp-...
```

</details>

## Commands

### Plugin Commands (in TUI)

| Command | Effect |
|---------|--------|
| `/slack-bridge:sync-on` | Start daemon + bind current session to Slack DM |
| `/slack-bridge:sync-off` | Mute TUI→Slack sync for current session |
| `/slack-bridge:start-daemon` | Start daemon only |
| `/slack-bridge:stop-daemon` | Stop daemon |
| `/slack-bridge:status` | Show status and active sessions |
| `/slack-bridge:logs` | View recent daemon logs |

### Slack Commands

| Command | Where | Effect |
|---------|-------|--------|
| `@bot <prompt>` | Channel | New session |
| `<message>` | DM | New session |
| Reply in thread | Thread | Continue session |
| `@bot resume <UUID>` | Channel | Bind TUI session to thread |
| `resume <UUID>` | DM | Bind TUI session to thread |
| `!stop` | Thread | Halt running session |
| `yolo off` | Thread | Disable YOLO/Trust mode |
| `sync off` / `sync on` | Thread | Mute/unmute TUI→Slack sync |

## Features

- **Bidirectional sync** — Slack messages forward into TUI via tmux, TUI responses sync back to Slack
- **Streaming responses** — live preview with cursor, final result overwrites progress
- **Phase-aware reactions** — emoji changes with processing stage (thinking → coding → browsing → done)
- **Thread status** — "is thinking...", "is using Bash" shown in Slack thread header
- **Timing footer** — "Finished in 2m 15s" posted after each response
- **!stop command** — type `!stop` in thread to halt a running session
- **OPTIONS buttons** — Claude can suggest choices as clickable Slack buttons
- **Markdown → mrkdwn** — tables, mermaid diagrams, code blocks properly converted
- **Dual-mode architecture** — Slack drives Claude via `--print` (PROCESS), or hooks sync TUI to Slack (HOOK)
- **Session persistence** — sessions survive restarts, resume from either side

## Config

`~/.claude/slack-bridge/config.json`:

```json
{
  "daemon_port": 7778,
  "work_dir": "/path/to/default/cwd",
  "claude_args": ["--tools", "Bash,Read,Write,Edit,Glob,Grep"],
  "max_concurrent_sessions": 3,
  "session_archive_after_secs": 3600
}
```

## Architecture

See [ARCHITECTURE.en.md](ARCHITECTURE.en.md) | [中文](ARCHITECTURE.md)

## Contributing

```bash
make install   # setup venv and install
make test      # run tests
make start     # start daemon
make stop      # stop daemon
```

## License

[MIT](LICENSE)
