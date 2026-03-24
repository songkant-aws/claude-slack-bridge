# Claude Slack Bridge v2 — 双模双向架构

## 核心理念

Daemon 同时支持两种模式，同一 Slack thread 贯穿整个 session 生命周期：
- **PROCESS 模式**：daemon 管理 `claude --print` 子进程，Slack 驱动（手机/远程）
- **HOOK 模式**：用户在本地 TUI，hooks 把 TUI 动作同步到 Slack
- **IDLE 模式**：两者都不活跃，等待唤醒

可随时 `claude --resume UUID` 切回 TUI，也可从 TUI 退出让 Slack 接管。

## Session 状态机

```
                    ┌─────────────────────────────┐
                    │         创建 Session          │
                    │  Slack @mention 或 hook 注册  │
                    └──────────────┬──────────────┘
                                   │
                                   ▼
                 ┌────────────────────────────────┐
        ┌───────│            PROCESS              │◄──── Slack 消息 (从 IDLE)
        │       │     daemon 管 claude --print     │
        │       │     Slack 消息 → stdin           │
        │       │     stdout → Slack thread        │
        │       └───────────────┬────────────────┘
        │                       │
        │  --print 进程退出      │  用户 claude --resume
        │  (TUI resume 抢占     │  (hook 注册到 daemon)
        │   或 idle 超时)        │
        │                       ▼
        │       ┌────────────────────────────────┐
        │       │             HOOK                │
        │       │     TUI hooks → daemon HTTP     │
        │       │     → Slack thread              │
        │       │                                 │
        │       │  Slack 消息 → ⚠️ 提示用户       │
        │       │  "Session 在 TUI 中，请在终端操作"│
        │       └───────────────┬────────────────┘
        │                       │
        │  TUI 退出              │  TUI 退出
        │  (hook stop 或超时)     │  (hook stop 或超时)
        │                       │
        ▼                       ▼
┌────────────────────────────────────────┐
│                IDLE                     │
│  两者都不活跃                            │
│                                         │
│  Slack 消息 → 启动 --print → PROCESS    │
│  TUI resume → hook 注册 → HOOK          │
│  超时 → ARCHIVED                        │
└────────────────────────────────────────┘
```

## 架构总览

```
┌──────────────────────────────────────────────────────────────────────────┐
│                           Cloud Desktop                                  │
│                                                                          │
│  ┌──────────────┐       ┌──────────────────────────────────────────────┐│
│  │  Slack App    │◄─────►│            Bridge Daemon                     ││
│  │  Socket Mode  │       │                                              ││
│  │               │       │  ┌───────────┐  ┌────────────┐              ││
│  │  events_api ──────────►  │ Msg Router │  │ Session    │              ││
│  │  interactive ─────────►  │            │  │ Manager    │              ││
│  │               │       │  │ thread_ts  │  │            │              ││
│  │  ◄── blocks ──│       │  │ → session  │  │ 状态机      │              ││
│  └──────────────┘       │  └─────┬──────┘  │ PROCESS    │              ││
│                          │        │         │ HOOK       │              ││
│                          │        │         │ IDLE       │              ││
│  ┌──────────────┐       │        │         └─────┬──────┘              ││
│  │  HTTP API     │       │        │               │                     ││
│  │  :7778        │       │        ▼               ▼                     ││
│  │               │       │  ┌──────────────────────────────────────┐   ││
│  │  /hooks/* ◄───────────│  │         Process Pool                  │   ││
│  │  (TUI hooks)  │       │  │                                       │   ││
│  │               │       │  │  ┌─ Session A (PROCESS) ────────────┐ │   ││
│  │  /sessions/* ◄────────│  │  │ claude --print --session-id UUID  │ │   ││
│  │  (管理接口)    │       │  │  │ stdin ◄── Slack msgs             │ │   ││
│  │               │       │  │  │ stdout ──► Slack thread           │ │   ││
│  └──────────────┘       │  │  └──────────────────────────────────┘ │   ││
│                          │  │                                       │   ││
│                          │  │  ┌─ Session B (HOOK) ───────────────┐ │   ││
│                          │  │  │ 无子进程，TUI hooks 推送到 Slack   │ │   ││
│                          │  │  └──────────────────────────────────┘ │   ││
│                          │  │                                       │   ││
│                          │  │  ┌─ Session C (IDLE) ───────────────┐ │   ││
│                          │  │  │ 等待 Slack 消息或 TUI resume      │ │   ││
│                          │  │  └──────────────────────────────────┘ │   ││
│                          │  └──────────────────────────────────────┘   ││
│                          └──────────────────────────────────────────────┘│
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  本地终端:                                                          │  │
│  │  $ claude --resume UUID-aaa   → TUI，daemon 切到 HOOK 模式         │  │
│  │  $ exit TUI                   → daemon 切到 IDLE 模式              │  │
│  │  Slack 来消息                  → daemon 启动 --print，切到 PROCESS  │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

## 数据流

### 1. 新 Session（Slack 发起 → PROCESS 模式）

```
用户在 Slack: "@bridge 帮我重构 auth 模块"
    │
    ▼
Daemon Socket Mode: app_mention
    │
    ├─► Session Mgr: 创建 session UUID-aaa, 状态=PROCESS
    │     ├─► 记录 channel_id + thread_ts ↔ UUID-aaa
    │     └─► 持久化到 sessions.json
    │
    ├─► Process Pool: 启动子进程
    │     claude --print \
    │       --session-id UUID-aaa \
    │       --name "重构auth模块" \
    │       --output-format stream-json \
    │       --input-format stream-json \
    │       -p "帮我重构 auth 模块"
    │
    └─► Slack: 发送 session header 到 thread
```

### 2. 流式响应（PROCESS 模式：Claude → Slack）

```
claude stdout (stream-json):
    {"type":"assistant","content":"让我先看看..."}
    {"type":"assistant","content":"让我先看看auth"}
    ...
    ▼
Daemon StreamReader (节流 1s)
    ├─► chat_postMessage (首次)
    ├─► chat_update (增量更新)
    └─► chat_postMessage (tool_use → 审批按钮)
```

### 3. Slack 回复 → 路由决策

```
用户在 thread 回复: "用 async/await 重写"
    │
    ▼
Daemon Socket Mode: events_api → message
    │
    ├─► 过滤: bot_id? subtype? → 跳过
    ├─► 查找: thread_ts → session UUID-aaa
    ├─► 检查 session.mode:
    │
    ├─ PROCESS → stdin.write({"type":"user","content":"用 async/await 重写"})
    │
    ├─ HOOK   → slack.post_text("⚠️ Session 正在 TUI 中使用，请在终端操作，
    │            或退出 TUI 后从 Slack 继续")
    │
    └─ IDLE   → 启动 claude --print --resume UUID-aaa
               → 切到 PROCESS 模式
               → stdin.write(message)
```

### 4. 工具审批（PROCESS 模式）

```
claude stdout: {"type":"tool_use","name":"Bash","input":{"command":"rm -rf old/"}}
    │
    ▼
Daemon: 检查 auto_approve_tools
    ├─ 白名单 → stdin 写 approved
    └─ 非白名单 → Slack 审批按钮
                    │
                    用户点 ✅ → stdin 写 approved
                    用户点 ❌ → stdin 写 rejected
```

### 5. TUI Resume（PROCESS/IDLE → HOOK 模式）

```
$ claude --resume UUID-aaa
    │
    ▼
TUI 启动，加载 session 历史
    │
    ├─► hooks.json 中配置的 user_prompt hook 触发
    │     → POST http://127.0.0.1:7778/hooks/user-prompt
    │     → daemon 收到，识别 session UUID-aaa
    │     → session.mode = HOOK
    │     → 如果有 --print 进程在跑，发 SIGTERM 终止
    │
    ├─► TUI 中用户操作 → hooks 同步到 Slack thread:
    │     user_prompt  → Slack 显示用户输入
    │     pre_tool_use → Slack 显示工具调用（可选审批）
    │     stop         → Slack 显示 Claude 回复
    │
    └─► TUI 退出 → stop hook 触发
          → daemon: session.mode = IDLE
```

### 6. TUI 退出 → Slack 接管

```
TUI 退出 (Ctrl+C 或 /exit)
    │
    ├─► stop hook → daemon: session.mode = IDLE
    │
    ▼
用户在 Slack thread 发消息
    │
    ├─► session.mode == IDLE
    ├─► 启动 claude --print --resume UUID-aaa
    ├─► session.mode = PROCESS
    └─► stdin.write(message)
```

## 模式检测机制

### PROCESS → HOOK 切换
```
daemon 检测到 hook 请求中的 session_id 匹配已有 session:
  1. 终止 --print 子进程 (SIGTERM)
  2. session.mode = HOOK
  3. 后续 hook 请求正常处理
```

### HOOK → IDLE 切换
```
两种触发:
  1. stop hook 收到 → session.mode = IDLE
  2. Hook 心跳超时 (5min 无 hook 请求) → session.mode = IDLE
```

### IDLE → PROCESS 切换
```
Slack thread 收到用户消息:
  1. 启动 claude --print --resume UUID
  2. session.mode = PROCESS
  3. 写入消息到 stdin
```

## 模块设计

```
src/claude_slack_bridge/
├── config.py            # 配置 (保留，扩展)
├── cli.py               # CLI: init/start/stop/status/hook (保留)
├── daemon.py            # 主守护进程 (重写)
│   ├── Socket Mode 连接 + 消息路由
│   ├── HTTP API (hook 端点)
│   └── 优雅关闭
├── session_manager.py   # Session 生命周期 + 状态机 (新)
│   ├── 三态: PROCESS / HOOK / IDLE
│   ├── thread_ts ↔ UUID 映射
│   ├── 模式切换逻辑
│   └── 持久化
├── process_pool.py      # Claude --print 子进程管理 (新)
│   ├── 启动/终止 claude --print
│   ├── stdin 写入
│   ├── stdout 流式读取
│   └── 进程退出回调
├── stream_parser.py     # stream-json 输出解析 (新)
│   ├── 解析 assistant/tool_use/tool_result/error
│   └── 触发对应 Slack 动作
├── slack_client.py      # Slack API 封装 (保留)
├── slack_formatter.py   # Block Kit 格式化 (保留，扩展)
├── approval.py          # 审批管理 (保留，适配双模式)
├── http_api.py          # Hook HTTP 端点 (保留，用于 TUI 模式)
└── hooks.py             # Hook CLI 入口 (保留，用于 TUI 模式)
```

## 配置

```json
{
  "default_channel": "claude-code",
  "max_concurrent_sessions": 3,
  "stream_throttle_ms": 1000,
  "auto_approve_tools": ["Read", "Glob", "Grep", "LS"],
  "require_approval": true,
  "approval_timeout_secs": 300,
  "idle_session_timeout_secs": 3600,
  "hook_heartbeat_timeout_secs": 300,
  "claude_args": ["--permission-mode", "default"]
}
```

## 与现有代码的关系

| 现有模块 | 处理方式 |
|---------|---------|
| config.py | 保留，加 hook_heartbeat_timeout_secs 等 |
| cli.py | 保留，hook 子命令继续用于 TUI 模式 |
| daemon.py | 重写，双模路由 + 进程管理 + HTTP API |
| slack_client.py | 保留 |
| slack_formatter.py | 保留，加流式更新 blocks |
| approval.py | 保留，PROCESS 模式写 stdin，HOOK 模式返回 HTTP |
| http_api.py | 保留，作为 HOOK 模式的接收端 |
| hooks.py | 保留，TUI hook CLI 入口 |
| registry.py | 合并到 session_manager.py |

## 典型使用场景

### 场景 1: 纯 Slack 使用（手机/远程）
```
@bridge 帮我修 bug → PROCESS 模式 → Slack 对话 → 完成
```

### 场景 2: Slack 开始，TUI 接管
```
@bridge 帮我重构 → PROCESS 模式 → Slack 对话
回到电脑 → claude --resume → HOOK 模式 → TUI 对话 (同步到 Slack)
```

### 场景 3: TUI 开始，Slack 继续
```
本地 claude --session-id UUID → HOOK 模式 → TUI 对话 (同步到 Slack)
离开电脑 → 退出 TUI → IDLE
手机 Slack 回复 → PROCESS 模式 → Slack 继续对话
```

### 场景 4: 来回切换
```
Slack 开始 → PROCESS
resume TUI → HOOK (Slack 消息被拦截提示)
退出 TUI → IDLE
Slack 继续 → PROCESS
再 resume → HOOK
...
```
