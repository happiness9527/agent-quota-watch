# Agent Quota Watch

Local quota snapshot monitoring and automatic resume scheduling for AI coding agents.

[English](#what-it-does) | [中文说明](#中文说明)

Agent Quota Watch helps long-running Codex, Claude Code, and Claude Desktop tasks survive quota windows. It watches local session logs and locally cached quota snapshots, records interrupted work, waits for the reset time, and resumes the task when the quota window opens again.

It is designed for people who start an agent job, hit a 5-hour quota limit, and do not want to wake up at 2:30 AM just to press continue.

![Agent Quota Watch dashboard](docs/screenshots/dashboard.jpg)

## What It Does

- Monitors Codex local `rate_limits` snapshots from `~/.codex/sessions`.
- Monitors Claude Code local session logs from `~/.claude/projects`.
- Reads Claude Desktop `/usage` cache snapshots when available.
- Scans Claude Desktop visible usage-limit text as a fallback on macOS.
- Registers interrupted or near-limit tasks with reset times.
- Resumes due Codex / Claude Code tasks through public CLI resume commands.
- For Claude Desktop, sends a continuation prompt in the current chat after the reset time.
- Provides a local web dashboard at `http://127.0.0.1:8765`.
- Supports a bilingual Dashboard UI with Chinese / English switching.
- Keeps macOS awake during unattended monitoring with `--keep-awake`.

## Important Scope

Agent Quota Watch is not an official quota API client.

Quota cards are local reference snapshots:

- Codex quota cards come from local session log `rate_limits`.
- Claude quota cards come from Claude Desktop Chromium `/usage` cache.
- These values can lag behind the UI shown by Codex, Claude, or Claude Code.
- If the platform UI disagrees with this dashboard, trust the platform UI.

The automatic resume workflow is the core feature. The quota percentages are helpful hints, not guaranteed real-time account balances.

## Quick Start

Clone the repo:

```bash
git clone https://github.com/happiness9527/agent-quota-watch.git
cd agent-quota-watch
```

Start the dashboard:

```bash
python3 quota_watch.py dashboard --scan-ui --open --keep-awake
```

Open:

```text
http://127.0.0.1:8765
```

Use the English Dashboard:

```text
http://127.0.0.1:8765/?lang=en
```

If `quota_watch.py` is not available in your copy, the legacy entry point also works:

```bash
python3 guardian.py dashboard --scan-ui --open --keep-awake
```

## Requirements

- macOS is recommended.
- Python 3.10+.
- Codex CLI if you want Codex resume support.
- Claude Code CLI if you want Claude Code resume support.
- Claude Desktop if you want Desktop app monitoring.
- Optional but recommended for Claude Desktop quota cache:

```bash
brew install zstd
```

For Claude Desktop window scanning, grant Accessibility permission to your terminal app:

1. Open macOS System Settings.
2. Go to Privacy & Security.
3. Open Accessibility.
4. Enable Terminal, iTerm, Warp, or the app that launches Python.

## How It Works

### Codex

Agent Quota Watch scans:

```text
~/.codex/sessions
~/.codex/archived_sessions
```

When a session log contains `rate_limits`, the dashboard can show local quota snapshots and reset times. If a task is already near the configured threshold, it can be scheduled for automatic resume after reset.

Resume command shape:

```bash
codex exec resume <session-id> "<resume prompt>"
```

### Claude Code

Agent Quota Watch scans:

```text
~/.claude/projects/**/*.jsonl
```

It looks for usage-limit signals such as `Usage limit reached`, `rate limited`, `retry after`, or related quota text. When it finds an interrupted session, it records the task and reset time.

Resume command shape:

```bash
claude -p --continue "<resume prompt>"
```

or:

```bash
claude -p --resume <session-id> "<resume prompt>"
```

### Claude Desktop

Agent Quota Watch uses two local-only sources:

- Claude Desktop `/usage` cache for quota snapshot cards.
- macOS Accessibility text scanning for visible usage-limit banners.

Claude Desktop's `Keep working` button is not treated as a resume button. It is an upgrade / extra-usage conversion entry. After the reset time, Agent Quota Watch refreshes the app and sends a continuation prompt in the current chat:

```text
请继续完成刚才因为额度限制中断的任务。请先简要回顾上一步已经完成到哪里，然后直接继续完成剩余工作。
```

## Common Commands

Start the dashboard:

```bash
python3 quota_watch.py dashboard --scan-ui --open --keep-awake
```

Scan once:

```bash
python3 quota_watch.py discover --scan-ui
```

List registered tasks:

```bash
python3 quota_watch.py list
```

Resume one task now:

```bash
python3 quota_watch.py resume <task-id>
```

Delete a task:

```bash
python3 quota_watch.py delete <task-id>
```

Adjust the early-warning threshold. This example schedules Claude Desktop when the local 5-hour snapshot has 15% or less remaining:

```bash
python3 quota_watch.py dashboard --scan-ui --quota-warning-remaining 15
```

Run as a terminal daemon:

```bash
python3 quota_watch.py daemon --scan-ui --interval 60
```

## Local State

By default, Agent Quota Watch writes local state to:

```text
~/.agent-quota-watch/
```

For compatibility, if the older `~/.agent-continuity/` directory already exists and `~/.agent-quota-watch/` does not, the tool will continue using the older directory.

Override the location:

```bash
AGENT_QUOTA_WATCH_HOME=/path/to/state python3 quota_watch.py dashboard
```

Project checkpoints are written inside the project directory:

```text
.agent-quota-watch/<task-id>.md
```

## Safety

- No account passwords are read.
- No API keys or auth files are parsed.
- No external network calls are needed for monitoring.
- Resume actions use public CLI commands or local macOS UI automation.
- Claude Desktop UI automation requires Accessibility permission.
- Local quota snapshots are treated as advisory data.

## 中文说明

Agent Quota Watch 是一个本地额度快照监控和自动续跑工具，适用于 Codex、Claude Code 和 Claude Desktop。

它解决的问题很具体：你让 AI agent 跑一个长任务，任务跑到一半遇到 5 小时额度限制，平台提示几个小时后才会重置。Agent Quota Watch 会记录这个任务，等待额度窗口恢复，并在到点后自动继续执行，避免你半夜起来手动点继续。

### 核心功能

- 监控 Codex 本地 session 日志里的 `rate_limits` 快照。
- 监控 Claude Code 本地 `~/.claude/projects` 会话日志。
- 读取 Claude Desktop 本地 `/usage` cache，展示 5 小时和周额度参考值。
- 在 macOS 上通过辅助功能权限扫描 Claude Desktop 可见额度提示。
- 自动登记额度中断或接近额度上限的任务。
- 到恢复时间后自动执行 Codex / Claude Code 的 CLI resume 命令。
- Claude Desktop 到点后会在当前对话里自动发送“继续刚才中断任务”的提示词。
- 提供本地可视化页面：`http://127.0.0.1:8765`。
- Dashboard 支持中文 / English 双语切换，方便国内和海外用户使用。
- 支持 `--keep-awake`，让 Mac 在无人值守时尽量不休眠。

### 重要说明

Agent Quota Watch 不是官方额度 API 客户端。

页面里的额度百分比是本机可读快照：

- Codex 额度来自本机 `~/.codex/sessions` 里的 `rate_limits`。
- Claude 额度来自 Claude Desktop 的 Chromium `/usage` cache。
- 这些数字可能落后于 Codex、Claude 或 Claude Code 界面显示。
- 如果平台界面和本工具显示不一致，以平台界面为准。

这个工具真正可靠的核心能力是：记录任务、等待 reset 时间、到点续跑。额度百分比只是辅助判断。

### 快速开始

克隆项目：

```bash
git clone https://github.com/happiness9527/agent-quota-watch.git
cd agent-quota-watch
```

启动可视化页面：

```bash
python3 quota_watch.py dashboard --scan-ui --open --keep-awake
```

浏览器打开：

```text
http://127.0.0.1:8765
```

英文界面：

```text
http://127.0.0.1:8765/?lang=en
```

如果你用的是旧版本文件，也可以用兼容入口：

```bash
python3 guardian.py dashboard --scan-ui --open --keep-awake
```

### 环境要求

- 推荐 macOS。
- Python 3.10 或更新版本。
- 如果要续跑 Codex，需要本机已安装 Codex CLI。
- 如果要续跑 Claude Code，需要本机已安装 Claude Code CLI。
- 如果要监控 Claude Desktop，需要安装 Claude Desktop。
- 推荐安装 `zstd`，用于读取 Claude Desktop 本地 `/usage` cache：

```bash
brew install zstd
```

### Claude Desktop 权限设置

如果需要扫描 Claude Desktop 窗口里的额度提示，需要给终端开启 macOS 辅助功能权限：

1. 打开 `系统设置`
2. 进入 `隐私与安全性`
3. 进入 `辅助功能`
4. 给 Terminal、iTerm、Warp 或启动 Python 的应用开启权限
5. 重新运行 Dashboard 命令

### 常用命令

启动 Dashboard：

```bash
python3 quota_watch.py dashboard --scan-ui --open --keep-awake
```

扫描一次：

```bash
python3 quota_watch.py discover --scan-ui
```

查看托管任务：

```bash
python3 quota_watch.py list
```

立即恢复某个任务：

```bash
python3 quota_watch.py resume <task-id>
```

删除误登记任务：

```bash
python3 quota_watch.py delete <task-id>
```

调整低额度预警阈值。下面表示 Claude Desktop 本地 5 小时快照剩余 15% 或更低时登记等待恢复：

```bash
python3 quota_watch.py dashboard --scan-ui --quota-warning-remaining 15
```

用终端 daemon 模式运行：

```bash
python3 quota_watch.py daemon --scan-ui --interval 60
```

### 数据保存位置

默认状态目录：

```text
~/.agent-quota-watch/
```

为了兼容早期版本，如果本机已经存在旧目录 `~/.agent-continuity/`，并且还没有 `~/.agent-quota-watch/`，工具会继续使用旧目录。

可以用环境变量覆盖状态目录：

```bash
AGENT_QUOTA_WATCH_HOME=/path/to/state python3 quota_watch.py dashboard
```

项目 checkpoint 会写到当前项目目录：

```text
.agent-quota-watch/<task-id>.md
```

### Claude Desktop 的 Keep working 按钮

Claude Desktop 里的 `Keep working` 不是继续任务按钮，而是升级或加购用量入口。Agent Quota Watch 不会把它当作续跑按钮。

到恢复时间后，工具会刷新 Claude Desktop，并在当前对话里自动发送：

```text
请继续完成刚才因为额度限制中断的任务。请先简要回顾上一步已经完成到哪里，然后直接继续完成剩余工作。
```

### 安全边界

- 不读取账号密码。
- 不解析 API Key 或认证文件。
- 监控逻辑不需要外部网络请求。
- 自动恢复使用公开 CLI 命令或本地 macOS UI 自动化。
- Claude Desktop UI 自动化需要辅助功能权限。
- 本地额度快照只作为参考值。

## Roadmap

- Better Claude Desktop visible UI parsing.
- Optional menu bar app.
- More agent platforms.
- Safer cross-device task state sync.
- Packaged macOS installer.

## License

MIT
