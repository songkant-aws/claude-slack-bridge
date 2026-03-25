---
description: Stop the Slack Bridge daemon
allowed-tools: [Bash]
---

Stop the Slack Bridge daemon. Run this exact command and report the result:

```bash
PID=$(cat ~/.claude/slack-bridge/daemon.pid 2>/dev/null)
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
    # Try port-based kill
    fuser -k 7778/tcp 2>/dev/null
    echo "✅ Slack Bridge daemon stopped (or was not running)"
else
    kill "$PID" 2>/dev/null
    sleep 2
    if kill -0 "$PID" 2>/dev/null; then
        kill -9 "$PID" 2>/dev/null
    fi
    echo "✅ Slack Bridge daemon stopped (PID $PID)"
fi
```

Report the result.
