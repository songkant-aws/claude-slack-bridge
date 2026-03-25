---
description: Show Slack Bridge daemon status and active sessions
allowed-tools: [Bash]
---

Check the Slack Bridge daemon status. Run this exact command and report the result:

```bash
if curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
    echo "✅ Daemon is running"
    echo ""
    echo "Active sessions:"
    curl -s http://127.0.0.1:7778/sessions | python3 -c "
import sys, json
sessions = json.load(sys.stdin)
active = [s for s in sessions if s['mode'] != 'idle']
idle = [s for s in sessions if s['mode'] == 'idle']
for s in active:
    print(f\"  🟢 {s['name'][:30]}  mode={s['mode']}  id={s['session_id'][:12]}\")
print(f\"  ({len(idle)} idle sessions)\")
if not active:
    print('  (no active sessions)')
"
else
    echo "❌ Daemon is not running. Use /slack-bridge to start it."
fi
```

Report the status clearly.
