# Agent Continuity Guardian

Local Python guardian for long Codex and Claude Code tasks that may be stopped
by quota or rate-limit windows.

It does not read private quota databases or auth files. It supervises tasks that
you start through this script, captures logs, detects quota messages, asks for
auto-resume approval, then resumes through the official local CLIs.

## Quick Start

Recommended unattended mode:

```bash
python guardian.py daemon
```

The daemon scans recent local Codex and Claude Code session records, registers
quota-limited tasks, waits until the reset time, then resumes them.

If you also want to scan the Claude desktop app window, macOS Accessibility
permission is required for Terminal/Python. Then run:

```bash
python guardian.py daemon --scan-ui
```

Manually run a Claude Code task through Guardian:

```bash
python guardian.py claude -- "finish the current project task, test it, and summarize the result"
```

Manually run a Codex task through Guardian:

```bash
python guardian.py codex -- "fix the current build issue and run the fastest relevant checks"
```

List registered tasks:

```bash
python guardian.py list
```

Auto-discover once without keeping a daemon open:

```bash
python guardian.py discover
```

Register an already interrupted latest Claude session manually:

```bash
python guardian.py add --platform claude --session last --retry-after 5h10m --auto-resume yes
```

## How It Works

- Claude start command: `claude -p ...`
- Claude resume command: `claude -p --continue ...` or `claude -p --resume <session>`
- Codex start command: `codex exec ...`
- Codex resume command: `codex exec resume --last ...` or `codex exec resume <session>`
- Runtime state: `~/.agent-continuity/tasks.json`
- Logs: `~/.agent-continuity/logs/`
- Project checkpoint files: `.agent-continuity/<task-id>.md`

Quota detection is heuristic. If the output contains words such as `quota`,
`rate limit`, `usage limit`, `limit reached`, `429`, or `try again in`, the task
is treated as rate-limited. If a retry duration can be parsed, Guardian uses it;
otherwise it retries after `5h10m`.

Codex session discovery can also read local `rate_limits.used_percent` and
`resets_at` fields from Codex rollout JSONL files. Claude Code discovery scans
recent `~/.claude/projects/**/*.jsonl` files for quota-limit messages. Claude
desktop app discovery is optional because macOS blocks window text access unless
Accessibility permission is granted.

## Useful Options

```bash
python guardian.py claude --auto-resume yes -- "long task"
python guardian.py codex --no-wait --auto-resume yes -- "long task"
python guardian.py daemon --once
python guardian.py discover --dry-run --json
python guardian.py delete <task-id>
python guardian.py update <task-id> --retry-at "2026-06-07T02:30:00+08:00"
python guardian.py resume <task-id>
python guardian.py show <task-id>
```

Use `--no-wait` when you want the current command to schedule an interrupted
task and exit. In that mode, keep `python guardian.py daemon` running elsewhere.
