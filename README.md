# Agent Continuity Guardian

这是一个本地守护工具，用来监测 Codex 和 Claude 的额度中断任务，并在额度恢复后自动继续。

它不会读取账号密码、token、私钥，也不会修改平台认证文件。它只读取本机已有的会话日志、Claude Desktop 本地 `/usage` 缓存和窗口文字，发现类似 `Usage limit reached`、`Resets 2:30 AM`、`rate limited` 的提示后，登记恢复时间。

## 推荐用法：可视化页面

进入工具目录：

```bash
cd "/Users/dbin/dev/sandbox/New project/agent-continuity-guardian"
```

启动可视化页面：

```bash
python3 guardian.py dashboard --scan-ui --open
```

打开后访问：

```text
http://127.0.0.1:8765
```

页面会显示：

- 当前有没有等待恢复的任务
- Codex / Claude 桌面 App 的 5h 与周额度剩余参考值
- 每个任务来自 Codex、Claude Code 还是 Claude 桌面 App
- 预计恢复时间
- 最近扫描状态
- 立即扫描、立即恢复、删除任务等操作

保持这个终端窗口打开，后台扫描才会继续运行。默认每 30 秒扫描一次。

如果你准备睡觉、希望 Mac 不休眠，可以这样启动：

```bash
python3 guardian.py dashboard --scan-ui --open --keep-awake
```

只要这个命令还在运行，macOS 会尽量保持唤醒；你按 `Ctrl+C` 停止后，守护页面也会停止。

## 你现在主要用 Codex 和 Claude 桌面 App

Codex：

- 直接用 `python3 guardian.py dashboard --scan-ui --open` 即可。
- 工具会扫描 `~/.codex/sessions` 里的本地会话记录。
- 如果 Codex 日志里有 `rate_limits.resets_at`，会自动读取真实恢复时间。
- 默认在 Codex 使用量达到 90% 左右时就登记为“等待重置”，不必等到彻底中断。
- 页面里会展示恢复时间和将要执行的 `codex exec resume ...` 命令。

注意：Codex 额度卡片读取的是本机 `~/.codex/sessions` 里最近一次 `rate_limits` 快照，不是官方实时余额接口。如果它和 Codex App 左下角显示不一致，以 Codex App 为准。

Claude 桌面 App：

```bash
python3 guardian.py dashboard --scan-ui --open
```

Guardian 会优先读取 Claude Desktop 本地 Chromium cache 里的 `/usage` 响应，用来显示 5 小时额度、周额度和重置时间。这部分不需要联网，但需要本机有 `zstd`：

```bash
brew install zstd
```

如果 Claude 5 小时额度剩余低于默认 `10%`，Guardian 会提前登记“等待重置”，到恢复时间后自动发送继续任务提示。你可以调整阈值：

```bash
python3 guardian.py dashboard --scan-ui --open --quota-warning-remaining 15
```

注意：Claude 额度卡片同样是本机 cache 快照，可能和 Claude / Claude Code 当前界面显示不同步。页面会显示快照时间；如果和平台 UI 不一致，以平台 UI 为准。

Claude 桌面 App 的 `Usage limit reached • Resets 2:30 AM • Keep working` 是窗口里的 UI 文字。macOS 默认不允许脚本读取其他 App 窗口，所以你需要打开权限：

1. 打开 `系统设置`
2. 进入 `隐私与安全性`
3. 进入 `辅助功能`
4. 给你运行命令的终端 App 开权限，比如 Terminal、iTerm、Warp，或 Python
5. 重新运行：

```bash
python3 guardian.py dashboard --scan-ui --open
```

如果没有这个权限，页面仍能运行，但 Claude 桌面 App 扫描会显示权限警告。

如果 Claude 桌面 App 的窗口文字读不到，把底部提示整句粘贴到页面里的“Claude 桌面 App 兜底登记”，例如：

```text
Usage limit reached • Resets 2:30 AM • Keep working
```

Guardian 会自动解析 `Resets 2:30 AM`，加 2 分钟缓冲，并登记自动恢复计划。

注意：Claude 桌面 App 里的 `Keep working` 不是“继续任务”按钮，而是引导升级/加购用量的入口。Guardian 不会把它当作续跑按钮使用。到恢复时间后，Guardian 会刷新 Claude 桌面 App，确认额度提示消失，然后在当前对话里自动发送：

```text
请继续完成刚才因为额度限制中断的任务。请先简要回顾上一步已经完成到哪里，然后直接继续完成剩余工作。
```

## 怎么确认到点真的会自动执行

看页面上的三个位置：

- 顶部“下一次恢复”：显示最近一个会自动恢复的具体时间。
- “自动恢复计划”：显示每个任务的恢复时间，以及到点要执行的动作。Claude 桌面 App 会显示“刷新 Claude 桌面 App，确认额度提示消失后，在当前对话自动发送继续任务提示”。
- 任务列表“操作”：如果显示“到点自动恢复”，说明 `auto_resume=true` 且后台 worker 会按时间检查。

后台 worker 默认每 30 秒检查一次。到达恢复时间后，它会执行计划里的命令，并把执行记录写到：

```text
~/.agent-continuity/logs/
```

如果你点击“立即扫描”后感觉没有反应，先看“最近操作结果”。那里会显示本次扫描发现了几个任务、恢复了几个任务、有没有权限警告。

## 为什么之前 `daemon` 看起来没有反应

`daemon` 是纯终端守护模式。旧版本没发现任务时几乎不输出，所以看起来像卡住。现在已经改成中文状态输出：

```bash
python3 guardian.py daemon
```

你会看到类似：

```text
Agent Guardian 守护进程已启动
扫描完成: 新发现 0 个，已恢复 0 个，等待恢复 0 个。
下次扫描将在 60 秒后执行。按 Ctrl+C 停止。
```

普通使用建议优先用可视化页面：

```bash
python3 guardian.py dashboard --scan-ui --open
```

## 常用命令

查看任务：

```bash
python3 guardian.py list
```

立即扫描一次：

```bash
python3 guardian.py discover
```

扫描 Claude 桌面 App：

```bash
python3 guardian.py discover --scan-ui
```

删除误登记任务：

```bash
python3 guardian.py delete <任务ID>
```

手动修改恢复时间：

```bash
python3 guardian.py update <任务ID> --retry-at "2026-06-07T02:30:00+08:00"
```

立即恢复某个任务：

```bash
python3 guardian.py resume <任务ID>
```

## 状态文件在哪里

任务状态：

```text
~/.agent-continuity/tasks.json
```

运行日志：

```text
~/.agent-continuity/logs/
```

项目 checkpoint：

```text
.agent-continuity/<任务ID>.md
```

## 重要限制

- Codex 的自动发现相对可靠，因为本地日志里有 `rate_limits` 字段。
- Claude Code CLI 可以扫描本地 `~/.claude/projects/**/*.jsonl`。
- Claude 桌面 App 需要 macOS 辅助功能权限，否则只能看见权限错误，不能读取 `Resets 2:30 AM`。
- 自动恢复不等于自动理解所有上下文。最稳的方式仍然是让 agent 在长任务中维护 checkpoint。
