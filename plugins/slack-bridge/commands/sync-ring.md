---
description: Silence sync chatter but keep Slack approval buttons
allowed-tools: [Bash]
---

Mute ambient TUI→Slack sync (prompts, progress, session lifecycle) for the
current session but keep permission requests posting to Slack. Useful when
you're on your phone and want to approve tools from Slack without the thread
filling up with sync chatter.

```bash
SESSION_ID=$("${CLAUDE_PLUGIN_ROOT}/bin/claude-slack-bridge-session-id" "$PWD")
if [ -z "$SESSION_ID" ]; then
    echo "❌ Could not resolve this TUI's session_id."
    echo "   The resolver walks up from pid $$ looking for ~/.claude/sessions/<pid>.json."
    exit 1
fi

RESULT=$(curl -s -X POST "http://127.0.0.1:7778/sessions/$SESSION_ID/mute" \
  -H "Content-Type: application/json" -d '{"level": "ring"}')
echo "$RESULT" | grep -q '"ok": true' && echo "🔕 Sync chatter muted — Slack approvals still active" || echo "⚠️ Failed: $RESULT"
```

Report the result.
