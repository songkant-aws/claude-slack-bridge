---
description: Fully mute current session (default) — nothing syncs, approvals go to TUI
allowed-tools: [Bash]
---

Drop the current session back to the default full-mute state. Nothing syncs
to Slack and permission requests fall back to the TUI's native approval
dialog. Use `/slack-bridge:sync-on` to opt back into sync, or
`/slack-bridge:sync-ring` to keep only Slack approvals.

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
  -H "Content-Type: application/json" -d '{"level": "none"}')
echo "$RESULT" | grep -q '"ok": true' && echo "🔇 TUI↔Slack sync fully muted" || echo "⚠️ Failed: $RESULT"
```

Report the result.
