---
description: Fully mute current session (default) — nothing syncs, approvals go to TUI
allowed-tools: [Bash]
---

Drop the current session back to the default full-mute state. Nothing syncs
to Slack and permission requests fall back to the TUI's native approval
dialog. Use `/slack-bridge:sync-on` to opt back into sync, or
`/slack-bridge:sync-ring` to keep only Slack approvals.

```bash
SESSION_ID=$("${CLAUDE_PLUGIN_ROOT}/bin/claude-slack-bridge-session-id" "$PWD")
if [ -z "$SESSION_ID" ]; then
    echo "❌ Could not resolve this TUI's session_id."
    echo "   The resolver walks up from pid $$ looking for ~/.claude/sessions/<pid>.json."
    exit 1
fi

RESULT=$(curl -s -X POST "http://127.0.0.1:7778/sessions/$SESSION_ID/mute" \
  -H "Content-Type: application/json" -d '{"level": "none"}')
echo "$RESULT" | grep -q '"ok": true' && echo "🔇 TUI↔Slack sync fully muted" || echo "⚠️ Failed: $RESULT"
```

Report the result.
