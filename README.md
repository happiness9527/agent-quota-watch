# Agent Continuity Guardian

Local Python guardian for long Codex and Claude Code tasks that may be stopped
by quota or rate-limit windows.

It does not read private quota databases or auth files. It supervises tasks that
you start through this script, captures logs, detects quota messages, asks for
auto-resume approval, then resumes through the official local CLIs.

## Quick Start

Run a Claude Code task:

```bash
python guardian.py claude -- "finish the current project task, test it, and summarize the result"
```

Run a Codex task:

```bash
python guardian.py codex -- "fix the current build issue and run the fastest relevant checks"
```

List registered tasks:

```bash
python guardian.py list
```

Register an already interrupted latest Claude session:

```bash
python guardian.py add --platform claude --session last --retry-after 5h10m --auto-resume yes
```

Run the background watcher:

```bash
python guardian.py daemon
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

## Useful Options

```bash
python guardian.py claude --auto-resume yes -- "long task"
python guardian.py codex --no-wait --auto-resume yes -- "long task"
python guardian.py daemon --once
python guardian.py resume <task-id>
python guardian.py show <task-id>
```

Use `--no-wait` when you want the current command to schedule an interrupted
task and exit. In that mode, keep `python guardian.py daemon` running elsewhere.

