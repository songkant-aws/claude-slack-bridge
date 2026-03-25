---
description: Start the Slack Bridge daemon and bind current session to a Slack DM thread
allowed-tools: [Bash]
---

Start the Slack Bridge daemon and bind the current TUI session to Slack.

## Step 1: Start daemon if not running

```bash
export LD_LIBRARY_PATH=/workplace/qianheng/MeshClaw/build/MeshClaw/MeshClaw-1.0/AL2_x86_64/DEV.STD.PTHREAD/build/private/tmp/brazil-path/build.libfarm/python3.10/lib:${LD_LIBRARY_PATH:-}

if curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
    echo "✅ Daemon already running"
else
    cd /workplace/qianheng/claude-slack-bridge
    setsid .venv/bin/python -m claude_slack_bridge.cli start >> ~/.claude/slack-bridge/daemon.log 2>&1 < /dev/null &
    sleep 3
    curl -s http://127.0.0.1:7778/health | grep -q ok && echo "✅ Daemon started" || echo "❌ Failed"
fi
```

## Step 2: Bind session

Find the most recently modified session file for the current working directory and bind it to Slack:

```bash
CWD_ENCODED=$(echo "$PWD" | sed 's|^/||; s|/|-|g')
SESSION_DIR="$HOME/.claude/projects/-${CWD_ENCODED}"
if [ ! -d "$SESSION_DIR" ]; then
    # Try parent dirs
    SESSION_DIR=$(ls -dt $HOME/.claude/projects/-* 2>/dev/null | head -1)
fi

SESSION_FILE=$(ls -t "$SESSION_DIR"/*.jsonl 2>/dev/null | head -1)
if [ -z "$SESSION_FILE" ]; then
    echo "⚠️ No session file found"
    exit 0
fi

SESSION_ID=$(basename "$SESSION_FILE" .jsonl)
echo "Found session: $SESSION_ID"

RESULT=$(curl -s -X POST http://127.0.0.1:7778/sessions/bind \
  -H "Content-Type: application/json" \
  -d "{\"session_id\": \"$SESSION_ID\", \"name\": \"TUI Session\", \"cwd\": \"$PWD\"}")

if echo "$RESULT" | grep -q '"ok"'; then
    echo "✅ Session bound to Slack DM"
    echo "   TUI ↔ Slack sync active"
else
    echo "⚠️ Bind failed: $RESULT"
fi
```

Report results of both steps.
