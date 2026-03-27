---
description: Show Slack Bridge daemon status and session details
allowed-tools: [Bash]
---

Check daemon status and show session details. Run:

```bash
if ! curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
    echo "❌ Daemon is not running. Use /start-daemon to start."
    exit 0
fi

echo "✅ Daemon running (PID $(cat ~/.claude/slack-bridge/daemon.pid 2>/dev/null))"
echo ""
curl -s http://127.0.0.1:7778/sessions | python3 -c "
import sys, json
from datetime import datetime
data = json.load(sys.stdin)
print(f'YOLO mode: {\"🟢 ON\" if data[\"yolo\"] else \"⚪ OFF\"}')
sessions = data['sessions']
process = [s for s in sessions if s['mode'] == 'process']
hook = [s for s in sessions if s['mode'] == 'hook']
idle = [s for s in sessions if s['mode'] == 'idle']
print(f'Sessions: {len(process)} process, {len(hook)} hook, {len(idle)} idle')
print()
for s in sessions:
    if s['mode'] == 'idle' and s['idle_secs'] > 3600:
        continue  # skip long-idle sessions
    mode_icon = {'process': '🟢', 'hook': '🔵', 'idle': '⚪'}.get(s['mode'], '?')
    flags = []
    if s.get('muted'): flags.append('🔇muted')
    if s.get('trusted'): flags.append('🔓trusted')
    flag_str = ' '.join(flags)
    created = datetime.fromtimestamp(s['created_at']).strftime('%m-%d %H:%M')
    age_m = s['age_secs'] // 60
    idle_m = s['idle_secs'] // 60
    print(f'{mode_icon} {s[\"name\"][:30]}')
    print(f'  ID: {s[\"session_id\"][:12]}  mode={s[\"mode\"]}  {flag_str}')
    print(f'  channel: {s[\"channel_id\"]}  thread: {s[\"thread_ts\"]}')
    print(f'  cwd: {s[\"cwd\"]}')
    print(f'  created: {created}  age: {age_m}m  idle: {idle_m}m')
    if s.get('pid'):
        init_ms = s.get('init_duration_ms', '?')
        print(f'  process PID: {s[\"pid\"]}  init: {init_ms}ms')
    print()
"
```

Report the status clearly.
