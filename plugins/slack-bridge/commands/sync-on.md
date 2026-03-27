---
description: Start TUI→Slack sync (starts daemon if needed, binds session, unmutes if muted)
allowed-tools: [Bash]
---

Enable TUI↔Slack sync for the current session. Run all steps:

```bash
export LD_LIBRARY_PATH=/workplace/qianheng/MeshClaw/build/MeshClaw/MeshClaw-1.0/AL2_x86_64/DEV.STD.PTHREAD/build/private/tmp/brazil-path/build.libfarm/python3.10/lib:${LD_LIBRARY_PATH:-}

# Step 1: Ensure daemon is running
if ! curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
    cd /workplace/qianheng/claude-slack-bridge
    setsid .venv/bin/python -m claude_slack_bridge.cli start >> ~/.claude/slack-bridge/daemon.log 2>&1 < /dev/null &
    sleep 3
    curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok && echo "✅ Daemon started" || { echo "❌ Daemon failed"; exit 1; }
else
    echo "✅ Daemon running"
fi

# Step 2: Find and bind session
CWD_ENCODED=$(echo "$PWD" | sed 's|^/||; s|/|-|g')
SESSION_DIR="$HOME/.claude/projects/-${CWD_ENCODED}"
[ ! -d "$SESSION_DIR" ] && SESSION_DIR=$(ls -dt $HOME/.claude/projects/-* 2>/dev/null | head -1)
SESSION_ID=$(basename "$(ls -t "$SESSION_DIR"/*.jsonl 2>/dev/null | head -1)" .jsonl 2>/dev/null)

if [ -z "$SESSION_ID" ]; then
    echo "⚠️ No session found"
    exit 0
fi

RESULT=$(curl -s -X POST http://127.0.0.1:7778/sessions/bind \
  -H "Content-Type: application/json" \
  -d "{\"session_id\": \"$SESSION_ID\", \"name\": \"TUI Session\", \"cwd\": \"$PWD\"}")
echo "$RESULT" | grep -q '"ok"' && echo "✅ Session $SESSION_ID bound to Slack" || echo "⚠️ Bind: $RESULT"

# Step 3: Unmute in case it was muted
curl -s -X POST "http://127.0.0.1:7778/sessions/$SESSION_ID/mute" \
  -H "Content-Type: application/json" -d '{"muted": false}' > /dev/null 2>&1
echo "🔊 TUI↔Slack sync active"
```

Report results.
