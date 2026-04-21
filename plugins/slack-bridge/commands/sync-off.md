---
description: Mute TUI↔Slack sync for current session (pass --ring to keep approval buttons)
allowed-tools: [Bash]
argument-hint: "[--ring]"
---

Mute TUI↔Slack sync for the current session. Default is `full` — nothing syncs
and permission requests fall back to the TUI's native dialog. Pass `--ring` to
silence ambient sync chatter but keep Slack approval buttons working.

Run:

```bash
LEVEL=full
for arg in $ARGUMENTS; do
    case "$arg" in
        --ring) LEVEL=ring ;;
    esac
done

CWD_ENCODED=$(echo "$PWD" | sed 's|^/||; s|/|-|g')
SESSION_DIR="$HOME/.claude/projects/-${CWD_ENCODED}"
[ ! -d "$SESSION_DIR" ] && SESSION_DIR=$(ls -dt $HOME/.claude/projects/-* 2>/dev/null | head -1)
SESSION_ID=$(basename "$(ls -t "$SESSION_DIR"/*.jsonl 2>/dev/null | head -1)" .jsonl 2>/dev/null)

if [ -z "$SESSION_ID" ]; then
    echo "⚠️ No session found"
    exit 0
fi

RESULT=$(curl -s -X POST "http://127.0.0.1:7778/sessions/$SESSION_ID/mute" \
  -H "Content-Type: application/json" -d "{\"level\": \"$LEVEL\"}")
if echo "$RESULT" | grep -q '"ok": true'; then
    if [ "$LEVEL" = "ring" ]; then
        echo "🔕 Sync chatter muted — Slack approvals still active"
    else
        echo "🔇 TUI↔Slack sync fully muted"
    fi
else
    echo "⚠️ Failed: $RESULT"
fi
```

Report the result.
