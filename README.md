# Agent Continuity Guardian

这是一个本地守护工具，用来监测 Codex 和 Claude 的额度中断任务，并在额度恢复后自动继续。

它不会读取账号密码、token、私钥，也不会修改平台认证文件。它只读取本机已有的会话日志和窗口文字，发现类似 `Usage limit reached`、`Resets 2:30 AM`、`rate limited` 的提示后，登记恢复时间。

## 推荐用法：可视化页面

进入工具目录：

```bash
cd "/Users/dbin/dev/sandbox/New project/agent-continuity-guardian"
```

启动可视化页面：

```bash
python3 guardian.py dashboard --open
```

打开后访问：

```text
http://127.0.0.1:8765
```

页面会显示：

- 当前有没有等待恢复的任务
- 每个任务来自 Codex、Claude Code 还是 Claude 桌面 App
- 预计恢复时间
- 最近扫描状态
- 立即扫描、立即恢复、删除任务等操作

保持这个终端窗口打开，后台扫描才会继续运行。默认每 30 秒扫描一次。

## 你现在主要用 Codex 和 Claude 桌面 App

Codex：

- 直接用 `python3 guardian.py dashboard --open` 即可。
- 工具会扫描 `~/.codex/sessions` 里的本地会话记录。
- 如果 Codex 日志里有 `rate_limits.resets_at`，会自动读取真实恢复时间。

Claude 桌面 App：

```bash
python3 guardian.py dashboard --scan-ui --open
```

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
python3 guardian.py dashboard --open
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
