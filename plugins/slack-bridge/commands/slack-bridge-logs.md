---
description: Show recent Slack Bridge daemon logs
allowed-tools: [Bash]
---

Show the last 30 lines of the Slack Bridge daemon log:

```bash
tail -30 ~/.claude/slack-bridge/daemon.log 2>/dev/null || echo "No log file found"
```

Summarize any errors or notable events.
