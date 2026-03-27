---
description: Mute TUI→Slack sync for current session
allowed-tools: [Bash]
---

Mute TUI→Slack sync for the current session. Run:

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
  -H "Content-Type: application/json" -d '{"muted": true}')
echo "$RESULT" | grep -q '"ok"' && echo "🔇 TUI→Slack sync muted" || echo "⚠️ Failed: $RESULT"
```

Report the result.
