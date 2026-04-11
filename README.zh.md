# Claude Slack Bridge

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Slack](https://img.shields.io/badge/Slack-Socket%20Mode-4A154B?logo=slack)](https://api.slack.com/apis/socket-mode)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-Plugin-orange?logo=anthropic)](https://docs.anthropic.com/en/docs/claude-code)

**从 Slack 远程操控 Claude Code。** 用手机发起会话，把 TUI 同步到线程，让团队实时围观 —— 类似 [Remote Control](https://code.claude.com/docs/en/remote-control) 或 [OpenClaw](https://github.com/openclaw/openclaw)，但通过你的团队已经在用的工具。

支持 Bedrock、API key 以及任何 Claude Code 配置。不需要 claude.ai 订阅。

[English](README.md) | 中文

<p align="center">
  <img src="docs/demo.gif" alt="Claude Slack Bridge Demo" width="600">
</p>

## 能用来做什么？

**通勤写代码** —— 在手机上私信 bot："把 auth 中间件重构成 JWT"。Claude 在你的云开发机上干活，到公司时工作已经完成 —— `claude --resume` 审查和迭代。

**开会时多线程工作** —— 在 Slack 里启动一个耗时任务（"迁移数据库 schema 并更新所有测试"），趁议程间隙看看进度。Claude 在后台持续工作，不需要你盯着。

**团队可见** —— 在项目频道 @提及 bot，整个团队都能在线程里看到 Claude 的工作过程 —— 适合做 demo、协作调试、或让队友了解进展。

**On-call 应急响应** —— 凌晨 2 点被告警叫醒？躺在床上私信 bot："查一下 /var/log/app 的错误日志，找出 root cause"。先在手机上 triage，再决定要不要爬起来。

**长时间任务** —— 从 Slack 启动一个大型重构，然后该干嘛干嘛。Slack 通知会告诉你 Claude 什么时候需要输入或者完成了。不需要维护一个终端会话。

## 工作流

### Slack 为主：手机远程控制

在 Slack 里私信或 @提及 bot，一个完整的 Claude Code 会话就在你的机器上启动 —— 读文件、改代码、跑测试。全部通过 Slack 完成。

```
1. @bot 或 DM    →  Claude Code 会话在你的机器上启动
2. 线程中对话    →  Claude 读写文件、执行命令，流式返回结果
3. 持续对话      →  多轮交互，完整工具访问
```

回到电脑前时，每个会话头部都附带一行恢复命令：

```bash
cd /your/project && claude --resume <session-id>
```

### TUI 为主：同步到 Slack

在电脑前正常使用 TUI。想把 Slack 当镜像的时候，随时 `/slack-bridge:sync-on` 就行。绑定之后一切自动同步。

```
1. 启动 TUI：          claude
2. 正常工作            （sync-on 随时都可以做）
3. /slack-bridge:sync-on → 会话绑定到 Slack DM 线程
4. 去吃午饭            →  掏出手机，在 Slack 线程里继续对话
5. 消息直达 TUI        →  通过 tmux send-keys 直接注入你的会话
6. Claude 回复          →  自动同步回 Slack
7. 回到工位            →  继续在 TUI 工作，完整上下文保留
```

## 与其他方案的区别

| | Remote Control | OpenClaw | **Claude Slack Bridge** |
|---|---|---|---|
| **客户端** | claude.ai / Claude App | WhatsApp、Telegram、Slack 等 | **Slack** |
| **认证** | claude.ai Pro/Max/Team/Enterprise | 自带 LLM key | **任何 Claude Code 配置（Bedrock、API key）** |
| **团队可见性** | 私人会话 | 单用户 | **频道共享，团队可以跟进** |
| **集成** | 独立 UI | 多渠道网关 | **原生 Slack（线程、@提及、通知）** |
| **会话交接** | Web ↔ TUI | N/A | **Slack ↔ TUI（`claude --resume`）** |

## 快速开始

```bash
# 1. 克隆并安装
git clone https://github.com/qianheng-aws/claude-slack-bridge.git
cd claude-slack-bridge && python3 -m venv .venv && .venv/bin/pip install -e .

# 2. 注册为 Claude Code 插件
claude plugins marketplace add /path/to/claude-slack-bridge
claude plugins install slack-bridge@qianheng-plugins

# 3. 初始化并添加 Slack token
.venv/bin/claude-slack-bridge init
# 编辑 ~/.claude/slack-bridge/.env 填入 SLACK_BOT_TOKEN 和 SLACK_APP_TOKEN
```

然后在 Claude Code TUI 中：
```
/slack-bridge:sync-on
```

<details>
<summary><b>Slack 应用配置</b></summary>

1. 在 https://api.slack.com/apps 创建应用
2. 启用 **Socket Mode**（生成 `xapp-` token）
3. 添加 **Bot Token Scopes**：`app_mentions:read`、`channels:history`、`channels:read`、`chat:write`、`im:history`、`im:read`、`reactions:write`
4. **Event Subscriptions** → 订阅 bot 事件：`app_mention`、`message.channels`、`message.im`
5. **Interactivity** → 启用（用于选项按钮）
6. **Assistant** → 在 app manifest 中启用 `assistant_view`，添加 `assistant:write` scope
7. **Event Subscriptions** → 额外订阅：`assistant_thread_started`、`assistant_thread_context_changed`
8. 安装应用到工作区，邀请 bot 进入频道

</details>

<details>
<summary><b>手动安装（不使用插件）</b></summary>

```bash
git clone https://github.com/qianheng-aws/claude-slack-bridge.git
cd claude-slack-bridge && python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/claude-slack-bridge init
# 编辑 ~/.claude/slack-bridge/.env:
#   SLACK_BOT_TOKEN=xoxb-...
#   SLACK_APP_TOKEN=xapp-...
```

</details>

## 命令

### 插件命令（TUI 中使用）

| 命令 | 效果 |
|------|------|
| `/slack-bridge:sync-on` | 启动守护进程 + 绑定当前会话到 Slack DM |
| `/slack-bridge:sync-off` | 静音当前会话的 TUI→Slack 同步 |
| `/slack-bridge:start-daemon` | 仅启动守护进程 |
| `/slack-bridge:stop-daemon` | 停止守护进程 |
| `/slack-bridge:status` | 显示状态和活跃会话 |
| `/slack-bridge:logs` | 查看最近的守护进程日志 |

### Slack 命令

| 命令 | 位置 | 效果 |
|------|------|------|
| `@bot <提示>` | 频道 | 新建会话 |
| `<消息>` | 私信 | 新建会话 |
| 线程回复 | 线程 | 继续会话 |
| `@bot resume <UUID>` | 频道 | 绑定 TUI 会话到线程 |
| `resume <UUID>` | 私信 | 绑定 TUI 会话到线程 |
| `!stop` | 线程 | 终止运行中的会话 |
| `yolo off` | 线程 | 关闭 YOLO/Trust 模式 |
| `sync off` / `sync on` | 线程 | 静音/恢复 TUI→Slack 同步 |

## 功能特性

- **双向同步** —— Slack 消息通过 tmux 转发到 TUI，TUI 响应自动同步回 Slack
- **流式响应** —— 实时预览带光标，最终结果覆盖进度消息
- **阶段感知 Reactions** —— emoji 随处理阶段变化（思考 → 编码 → 浏览 → 完成）
- **线程状态** —— Slack 线程头显示 "is thinking..."、"is using Bash"
- **计时 Footer** —— 每次响应后显示 "Finished in 2m 15s"
- **!stop 命令** —— 在线程中输入 `!stop` 终止运行中的会话
- **选项按钮** —— Claude 可以将选项渲染为 Slack 可点击按钮
- **Markdown → mrkdwn** —— 表格、Mermaid 图、代码块正确转换
- **双模架构** —— Slack 通过 `--print` 驱动 Claude（PROCESS），或 hooks 将 TUI 同步到 Slack（HOOK）
- **会话持久化** —— 会话在重启后保留，任一端可恢复

## 配置

`~/.claude/slack-bridge/config.json`：

```json
{
  "daemon_port": 7778,
  "work_dir": "/path/to/default/cwd",
  "claude_args": ["--tools", "Bash,Read,Write,Edit,Glob,Grep"],
  "max_concurrent_sessions": 3,
  "session_archive_after_secs": 3600
}
```

## 架构

详见 [ARCHITECTURE.md](ARCHITECTURE.md) | [English](ARCHITECTURE.en.md)

## 贡献

```bash
make install   # 安装依赖
make test      # 运行测试
make start     # 启动守护进程
make stop      # 停止守护进程
```

## 许可证

[MIT](LICENSE)
