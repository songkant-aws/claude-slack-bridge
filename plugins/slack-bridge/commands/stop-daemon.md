---
description: Stop the Slack Bridge daemon
allowed-tools: [Bash]
---

Stop the Slack Bridge daemon. Sessions are auto-persisted to disk so no data is lost.

```bash
PID=$(cat ~/.claude/slack-bridge/daemon.pid 2>/dev/null)
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
    fuser -k 7778/tcp 2>/dev/null
    echo "✅ Daemon stopped (or was not running)"
else
    kill "$PID" 2>/dev/null
    sleep 2
    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null
    echo "✅ Daemon stopped (PID $PID)"
fi
```

Report the result.
