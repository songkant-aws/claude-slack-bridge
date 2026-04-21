---
description: Silence sync chatter but keep Slack approval buttons
allowed-tools: [Bash]
---

Mute ambient TUI→Slack sync (prompts, progress, session lifecycle) for the
current session but keep permission requests posting to Slack. Useful when
you're on your phone and want to approve tools from Slack without the thread
filling up with sync chatter.

```bash
CWD_ENCODED=$(echo "$PWD" | sed 's|^/||; s|/|-|g')
SESSION_DIR="$HOME/.claude/projects/-${CWD_ENCODED}"
[ ! -d "$SESSION_DIR" ] && SESSION_DIR=$(ls -dt $HOME/.claude/projects/-* 2>/dev/null | head -1)
SESSION_ID=$(basename "$(ls -t "$SESSION_DIR"/*.jsonl 2>/dev/null | head -1)" .jsonl 2>/dev/null)

if [ -z "$SESSION_ID" ]; then
    echo "⚠️ No session found"
    exit 0
fi

RESULT=$(curl -s -X POST "http://127.0.0.1:7778/sessions/$SESSION_ID/mute" \
  -H "Content-Type: application/json" -d '{"level": "ring"}')
echo "$RESULT" | grep -q '"ok": true' && echo "🔕 Sync chatter muted — Slack approvals still active" || echo "⚠️ Failed: $RESULT"
```

Report the result.
