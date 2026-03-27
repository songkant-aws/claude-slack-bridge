---
description: Start the Slack Bridge daemon
allowed-tools: [Bash]
---

Start the Slack Bridge daemon. Run:

```bash
export LD_LIBRARY_PATH=/workplace/qianheng/MeshClaw/build/MeshClaw/MeshClaw-1.0/AL2_x86_64/DEV.STD.PTHREAD/build/private/tmp/brazil-path/build.libfarm/python3.10/lib:${LD_LIBRARY_PATH:-}

if curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
    echo "✅ Daemon already running (PID $(cat ~/.claude/slack-bridge/daemon.pid 2>/dev/null))"
else
    cd /workplace/qianheng/claude-slack-bridge
    setsid .venv/bin/python -m claude_slack_bridge.cli start >> ~/.claude/slack-bridge/daemon.log 2>&1 < /dev/null &
    sleep 3
    if curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
        echo "✅ Daemon started (PID $(cat ~/.claude/slack-bridge/daemon.pid 2>/dev/null))"
    else
        echo "❌ Failed to start. Check: tail -10 ~/.claude/slack-bridge/daemon.log"
    fi
fi
```

Report the result.
