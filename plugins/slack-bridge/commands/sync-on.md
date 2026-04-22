---
description: Start TUI→Slack sync (starts daemon if needed, binds session, unmutes if muted)
allowed-tools: [Bash]
---

Enable TUI↔Slack sync for the current session. Run all steps:

```bash
# Step 1: Ensure daemon is running
export PATH="$HOME/.local/bin:$PATH"
if ! command -v claude-slack-bridge >/dev/null 2>&1; then
    echo "❌ claude-slack-bridge not found. Run the Quick Start from the README to install it."
    exit 1
fi

if ! curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
    setsid claude-slack-bridge start >> ~/.claude/slack-bridge/daemon.log 2>&1 < /dev/null &
    sleep 3
    curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok && echo "✅ Daemon started" || { echo "❌ Daemon failed"; exit 1; }
else
    echo "✅ Daemon running"
fi

# Step 2: Resolve the real session_id of *this* TUI. Claude Code writes
# ~/.claude/sessions/<pid>.json — walking up our parent chain finds it.
# No mtime fallback: picking "newest jsonl in cwd" silently binds to the
# wrong session when the cwd has parallel/older sessions. Fail loudly.
SESSION_ID=$("${CLAUDE_PLUGIN_ROOT}/bin/claude-slack-bridge-session-id" "$PWD")
if [ -z "$SESSION_ID" ]; then
    echo "❌ Could not resolve this TUI's session_id."
    echo "   The resolver walks up from pid $$ looking for ~/.claude/sessions/<pid>.json."
    echo "   If this keeps happening, file an issue with the output of:"
    echo "     ls ~/.claude/sessions/; ps -o pid,ppid,comm -p \$\$ --forest"
    exit 1
fi

RESULT=$(curl -s -X POST http://127.0.0.1:7778/sessions/bind \
  -H "Content-Type: application/json" \
  -d "{\"session_id\": \"$SESSION_ID\", \"name\": \"TUI-${SESSION_ID:0:12}\", \"cwd\": \"$PWD\", \"tmux_pane_id\": \"${TMUX_PANE:-}\"}")
echo "$RESULT" | grep -q '"ok"' && echo "✅ Session $SESSION_ID bound to Slack" || echo "⚠️ Bind: $RESULT"

# Step 3: Mark session as explicitly opted-in to sync
curl -s -X POST "http://127.0.0.1:7778/sessions/$SESSION_ID/mute" \
  -H "Content-Type: application/json" -d '{"level": "sync"}' > /dev/null 2>&1
echo "🔊 TUI↔Slack sync active"
```

Report results.
