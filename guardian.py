#!/usr/bin/env python3
"""Local continuity guardian for Codex and Claude Code tasks.

The script intentionally uses only public CLI resume commands and local state.
It does not read platform auth files or private quota databases.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


APP_NAME = "Agent Guardian"
DEFAULT_HOME = Path(
    os.environ.get("AGENT_GUARDIAN_HOME", Path.home() / ".agent-continuity")
).expanduser()
TASKS_FILE = DEFAULT_HOME / "tasks.json"
LOG_DIR = DEFAULT_HOME / "logs"
DEFAULT_RETRY_DELAY = timedelta(hours=5, minutes=10)
DEFAULT_WARN_AFTER = timedelta(hours=4, minutes=30)

QUOTA_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\brate limit(?:ed|s)?\b",
        r"\busage limit\b",
        r"\bquota\b",
        r"\blimit reached\b",
        r"\busage cap\b",
        r"\b429\b",
        r"try again in",
        r"retry after",
        r"available again",
    )
]

DURATION_TOKEN_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>d(?:ays?)?|h(?:ours?|rs?)?|m(?:in(?:ute)?s?)?|s(?:ec(?:ond)?s?)?)",
    re.IGNORECASE,
)


@dataclass
class RunResult:
    status: str
    exit_code: int | None
    quota_detected: bool
    retry_at: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat(timespec="seconds")


def parse_datetime(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def parse_duration(value: str) -> timedelta:
    """Parse compact durations such as 5h10m, 30m, 1 hour 20 minutes."""
    value = value.strip()
    if not value:
        raise ValueError("duration is empty")

    total = timedelta()
    matched = False
    for match in DURATION_TOKEN_RE.finditer(value):
        matched = True
        amount = float(match.group("value"))
        unit = match.group("unit").lower()
        if unit.startswith("d"):
            total += timedelta(days=amount)
        elif unit.startswith("h"):
            total += timedelta(hours=amount)
        elif unit.startswith("m"):
            total += timedelta(minutes=amount)
        elif unit.startswith("s"):
            total += timedelta(seconds=amount)

    if not matched:
        raise ValueError(f"could not parse duration: {value}")
    return total


def strip_leading_separator(parts: list[str]) -> list[str]:
    if parts and parts[0] == "--":
        return parts[1:]
    return parts


def prompt_from_parts(parts: list[str]) -> str:
    parts = strip_leading_separator(parts)
    return " ".join(parts).strip()


def ensure_home() -> None:
    DEFAULT_HOME.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_tasks() -> list[dict[str, Any]]:
    ensure_home()
    if not TASKS_FILE.exists():
        return []
    with TASKS_FILE.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"invalid task store: {TASKS_FILE}")
    return data


def save_tasks(tasks: list[dict[str, Any]]) -> None:
    ensure_home()
    tmp = TASKS_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(tasks, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(TASKS_FILE)


def upsert_task(task: dict[str, Any]) -> None:
    tasks = load_tasks()
    for idx, existing in enumerate(tasks):
        if existing["id"] == task["id"]:
            tasks[idx] = task
            break
    else:
        tasks.append(task)
    save_tasks(tasks)


def get_task(task_id: str) -> dict[str, Any]:
    for task in load_tasks():
        if task["id"] == task_id:
            return task
    raise SystemExit(f"task not found: {task_id}")


def task_id_for(platform: str, cwd: str) -> str:
    slug = Path(cwd).name.lower().replace(" ", "-") or "task"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{platform}-{slug}-{stamp}-{uuid.uuid4().hex[:6]}"


def make_task(
    *,
    platform: str,
    cwd: str,
    prompt: str,
    name: str | None,
    session: str,
    auto_resume: bool | None,
    retry_at: str | None = None,
) -> dict[str, Any]:
    cwd_path = str(Path(cwd).expanduser().resolve())
    task_id = task_id_for(platform, cwd_path)
    checkpoint_path = str(Path(cwd_path) / ".agent-continuity" / f"{task_id}.md")
    created_at = iso(local_now())
    return {
        "id": task_id,
        "name": name or task_id,
        "platform": platform,
        "cwd": cwd_path,
        "session": session,
        "prompt": prompt,
        "status": "scheduled" if retry_at else "created",
        "auto_resume": auto_resume,
        "retry_at": retry_at,
        "attempts": 0,
        "created_at": created_at,
        "updated_at": created_at,
        "last_started_at": None,
        "last_exit_code": None,
        "log_path": str(LOG_DIR / f"{task_id}.log"),
        "checkpoint_path": checkpoint_path,
    }


def update_task(task: dict[str, Any], **changes: Any) -> dict[str, Any]:
    task.update(changes)
    task["updated_at"] = iso(local_now())
    upsert_task(task)
    return task


def ensure_checkpoint(task: dict[str, Any]) -> None:
    path = Path(task["checkpoint_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    content = (
        f"# Agent Continuity Checkpoint\n\n"
        f"- Task ID: `{task['id']}`\n"
        f"- Platform: `{task['platform']}`\n"
        f"- CWD: `{task['cwd']}`\n"
        f"- Status: created\n\n"
        "## Original Task\n\n"
        f"{task.get('prompt') or '(manual resume task)'}\n\n"
        "## Current State\n\n"
        "- Not started by guardian yet.\n\n"
        "## Next Action\n\n"
        "- Read this file, inspect the repository state, then continue only the unfinished work.\n"
    )
    path.write_text(content, encoding="utf-8")


def wrapped_start_prompt(task: dict[str, Any]) -> str:
    return (
        "你正在由 Agent Guardian 托管执行一个可能跨额度窗口的长任务。\n"
        f"Checkpoint 文件: {task['checkpoint_path']}\n\n"
        "执行要求:\n"
        "1. 开始前先检查当前目录和 git status。\n"
        "2. 在关键阶段更新 checkpoint，写明已完成、未完成、下一步和验证结果。\n"
        "3. 如果遇到 quota/rate limit/usage limit，停止前尽量更新 checkpoint。\n"
        "4. 完成后给出简短总结和验证命令结果。\n\n"
        f"用户任务:\n{task['prompt']}"
    )


def wrapped_resume_prompt(task: dict[str, Any]) -> str:
    return (
        "继续 Agent Guardian 托管的未完成任务。\n"
        f"Checkpoint 文件: {task['checkpoint_path']}\n\n"
        "恢复要求:\n"
        "1. 先读取 checkpoint，并检查 git status。\n"
        "2. 简短判断已完成和剩余工作。\n"
        "3. 只继续未完成项，不重做无关工作。\n"
        "4. 完成后更新 checkpoint，说明验证命令和结果。\n"
    )


def build_command(task: dict[str, Any], *, resume: bool) -> list[str]:
    platform = task["platform"]
    session = task.get("session") or "last"
    if platform == "claude":
        if resume:
            if session != "last":
                return ["claude", "-p", "--resume", session, wrapped_resume_prompt(task)]
            return ["claude", "-p", "--continue", wrapped_resume_prompt(task)]
        return ["claude", "-p", wrapped_start_prompt(task)]

    if platform == "codex":
        if resume:
            if session != "last":
                return ["codex", "exec", "resume", session, wrapped_resume_prompt(task)]
            return ["codex", "exec", "resume", "--last", wrapped_resume_prompt(task)]
        return ["codex", "exec", wrapped_start_prompt(task)]

    raise ValueError(f"unsupported platform: {platform}")


def is_quota_text(text: str) -> bool:
    return any(pattern.search(text) for pattern in QUOTA_PATTERNS)


def extract_duration_after_keyword(text: str) -> timedelta | None:
    lowered = text.lower()
    keyword_positions = [
        lowered.find(keyword)
        for keyword in (
            "try again in",
            "retry in",
            "retry after",
            "reset in",
            "available again in",
            "wait",
        )
        if lowered.find(keyword) != -1
    ]
    if not keyword_positions:
        return None

    start = min(keyword_positions)
    window = text[start : start + 160]
    try:
        return parse_duration(window)
    except ValueError:
        return None


def infer_retry_at(text: str, *, now: datetime | None = None) -> datetime:
    now = now or local_now()
    duration = extract_duration_after_keyword(text)
    return now + (duration or DEFAULT_RETRY_DELAY)


def applescript_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def ask_confirmation(message: str, *, default: bool, no_popup: bool) -> bool:
    if no_popup:
        return default

    if sys.platform == "darwin" and shutil.which("osascript"):
        script = (
            f'display dialog {applescript_quote(message)} '
            'buttons {"否", "是"} default button "是" '
            f'with title {applescript_quote(APP_NAME)} with icon caution'
        )
        result = subprocess.run(
            ["osascript", "-e", script],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return "button returned:是" in result.stdout
        return default

    if sys.stdin.isatty():
        suffix = "Y/n" if default else "y/N"
        answer = input(f"{message} [{suffix}] ").strip().lower()
        if not answer:
            return default
        return answer in {"y", "yes", "是", "确认", "ok"}

    return default


def write_log_header(task: dict[str, Any], command: list[str], *, resume: bool) -> None:
    log_path = Path(task["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "resume" if resume else "start"
    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n")
        log.write("=" * 80 + "\n")
        log.write(f"{iso(local_now())} {mode} task {task['id']}\n")
        log.write(f"cwd: {task['cwd']}\n")
        log.write(f"command: {json.dumps(command, ensure_ascii=False)}\n")
        log.write("=" * 80 + "\n")


def execute_task(
    task: dict[str, Any],
    *,
    resume: bool,
    warn_after: timedelta,
    no_popup: bool,
) -> RunResult:
    ensure_checkpoint(task)
    command = build_command(task, resume=resume)
    write_log_header(task, command, resume=resume)
    update_task(
        task,
        status="resuming" if resume else "running",
        attempts=int(task.get("attempts") or 0) + 1,
        last_started_at=iso(local_now()),
    )

    quota_detected = False
    warned = False
    tail = ""
    started = time.monotonic()
    log_path = Path(task["log_path"])

    try:
        process = subprocess.Popen(
            command,
            cwd=task["cwd"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"command not found: {exc}\n")
        update_task(task, status="failed", last_exit_code=127)
        return RunResult(status="failed", exit_code=127, quota_detected=False)

    assert process.stdout is not None
    with log_path.open("a", encoding="utf-8") as log:
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
            log.flush()
            tail = (tail + line)[-20000:]
            if is_quota_text(line):
                quota_detected = True
            if (
                not warned
                and task.get("auto_resume") is None
                and warn_after.total_seconds() >= 0
                and time.monotonic() - started >= warn_after.total_seconds()
            ):
                approved = ask_confirmation(
                    "当前托管任务运行时间较长，后续如果额度耗尽，是否自动等待重置后继续？",
                    default=True,
                    no_popup=no_popup,
                )
                update_task(task, auto_resume=approved)
                warned = True

    exit_code = process.wait()
    if is_quota_text(tail):
        quota_detected = True

    if quota_detected:
        retry_at = infer_retry_at(tail)
        update_task(
            task,
            status="rate_limited",
            retry_at=iso(retry_at),
            last_exit_code=exit_code,
        )
        return RunResult(
            status="rate_limited",
            exit_code=exit_code,
            quota_detected=True,
            retry_at=iso(retry_at),
        )

    status = "completed" if exit_code == 0 else "failed"
    update_task(task, status=status, retry_at=None, last_exit_code=exit_code)
    return RunResult(status=status, exit_code=exit_code, quota_detected=False)


def sleep_until(target: datetime) -> None:
    while True:
        remaining = (target - local_now()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 60))


def run_with_continuity(
    task: dict[str, Any],
    *,
    warn_after: timedelta,
    no_popup: bool,
    foreground_wait: bool,
) -> int:
    result = execute_task(task, resume=False, warn_after=warn_after, no_popup=no_popup)
    while result.status == "rate_limited":
        if task.get("auto_resume") is None:
            approved = ask_confirmation(
                "任务因为额度限制中断。是否自动等待额度重置后继续？",
                default=True,
                no_popup=no_popup,
            )
            update_task(task, auto_resume=approved)

        if not task.get("auto_resume"):
            print(f"已记录任务，但未开启自动恢复: {task['id']}")
            return result.exit_code or 1

        retry_at = parse_datetime(result.retry_at or task["retry_at"])
        print(f"任务已进入等待恢复队列: {task['id']}")
        print(f"预计恢复时间: {iso(retry_at)}")

        if not foreground_wait:
            print("请保持 guardian daemon 运行，或之后执行 resume 命令。")
            return 0

        sleep_until(retry_at)
        result = execute_task(task, resume=True, warn_after=warn_after, no_popup=no_popup)

    return result.exit_code or 0


def parse_auto_resume(value: str) -> bool | None:
    if value == "ask":
        return None
    if value == "yes":
        return True
    if value == "no":
        return False
    raise ValueError(value)


def cmd_run(args: argparse.Namespace) -> int:
    prompt = prompt_from_parts(args.prompt)
    if not prompt:
        raise SystemExit("missing task prompt")

    task = make_task(
        platform=args.platform,
        cwd=args.cwd,
        prompt=prompt,
        name=args.name,
        session=args.session,
        auto_resume=parse_auto_resume(args.auto_resume),
    )
    upsert_task(task)
    print(f"已登记任务: {task['id']}")
    print(f"日志: {task['log_path']}")
    print(f"Checkpoint: {task['checkpoint_path']}")
    return run_with_continuity(
        task,
        warn_after=parse_duration(args.warn_after),
        no_popup=args.no_popup,
        foreground_wait=not args.no_wait,
    )


def cmd_add(args: argparse.Namespace) -> int:
    retry_at = None
    if args.retry_at:
        retry_at = iso(parse_datetime(args.retry_at))
    elif args.retry_after:
        retry_at = iso(local_now() + parse_duration(args.retry_after))

    task = make_task(
        platform=args.platform,
        cwd=args.cwd,
        prompt=args.prompt or "",
        name=args.name,
        session=args.session,
        auto_resume=parse_auto_resume(args.auto_resume),
        retry_at=retry_at,
    )
    if retry_at:
        task["status"] = "rate_limited"
    upsert_task(task)
    ensure_checkpoint(task)
    print(f"已补登记任务: {task['id']}")
    if retry_at:
        print(f"预计恢复时间: {retry_at}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    tasks = load_tasks()
    if args.json:
        print(json.dumps(tasks, ensure_ascii=False, indent=2))
        return 0

    if not tasks:
        print("暂无托管任务。")
        return 0

    for task in tasks:
        print(
            f"{task['id']}  "
            f"{task['platform']}  "
            f"{task['status']}  "
            f"auto={task.get('auto_resume')}  "
            f"retry_at={task.get('retry_at') or '-'}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    task = get_task(args.task_id)
    print(json.dumps(task, ensure_ascii=False, indent=2))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    task = get_task(args.task_id)
    result = execute_task(
        task,
        resume=True,
        warn_after=parse_duration(args.warn_after),
        no_popup=args.no_popup,
    )
    return result.exit_code or 0


def due_for_resume(task: dict[str, Any]) -> bool:
    if task.get("status") not in {"rate_limited", "scheduled"}:
        return False
    if not task.get("auto_resume"):
        return False
    retry_at = task.get("retry_at")
    if not retry_at:
        return True
    return parse_datetime(retry_at) <= local_now()


def daemon_tick(args: argparse.Namespace) -> None:
    for task in load_tasks():
        if not due_for_resume(task):
            continue
        print(f"开始恢复任务: {task['id']}")
        result = execute_task(
            task,
            resume=True,
            warn_after=parse_duration(args.warn_after),
            no_popup=args.no_popup,
        )
        if result.status == "rate_limited":
            print(f"任务仍受额度限制，已重新排队: {task['id']} -> {result.retry_at}")
        else:
            print(f"任务恢复结束: {task['id']} status={result.status}")


def cmd_daemon(args: argparse.Namespace) -> int:
    print(f"{APP_NAME} daemon started. state={TASKS_FILE}")
    while True:
        daemon_tick(args)
        if args.once:
            return 0
        time.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="guardian",
        description="Keep Codex and Claude Code tasks resumable across quota windows.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start a supervised agent task")
    run.add_argument("platform", choices=["claude", "codex"])
    run.add_argument("prompt", nargs="*")
    run.add_argument("--cwd", default=os.getcwd())
    run.add_argument("--name")
    run.add_argument("--session", default="last")
    run.add_argument("--auto-resume", choices=["ask", "yes", "no"], default="ask")
    run.add_argument("--warn-after", default="4h30m")
    run.add_argument("--no-popup", action="store_true")
    run.add_argument(
        "--no-wait",
        action="store_true",
        help="schedule quota-limited tasks but do not wait in the foreground",
    )
    run.set_defaults(func=cmd_run)

    add = sub.add_parser("add", help="register an existing interrupted session")
    add.add_argument("--platform", choices=["claude", "codex"], required=True)
    add.add_argument("--cwd", default=os.getcwd())
    add.add_argument("--session", default="last")
    add.add_argument("--name")
    add.add_argument("--prompt")
    add.add_argument("--auto-resume", choices=["ask", "yes", "no"], default="yes")
    add.add_argument("--retry-after")
    add.add_argument("--retry-at")
    add.set_defaults(func=cmd_add)

    list_cmd = sub.add_parser("list", help="list supervised tasks")
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="show one task as JSON")
    show.add_argument("task_id")
    show.set_defaults(func=cmd_show)

    resume = sub.add_parser("resume", help="resume one registered task now")
    resume.add_argument("task_id")
    resume.add_argument("--warn-after", default="4h30m")
    resume.add_argument("--no-popup", action="store_true")
    resume.set_defaults(func=cmd_resume)

    daemon = sub.add_parser("daemon", help="watch scheduled tasks and resume due ones")
    daemon.add_argument("--interval", type=int, default=60)
    daemon.add_argument("--once", action="store_true")
    daemon.add_argument("--warn-after", default="4h30m")
    daemon.add_argument("--no-popup", action="store_true")
    daemon.set_defaults(func=cmd_daemon)

    return parser


def normalize_shorthand(argv: list[str]) -> list[str]:
    if argv and argv[0] in {"claude", "codex"}:
        return ["run", *argv]
    return argv


def main(argv: list[str] | None = None) -> int:
    argv = normalize_shorthand(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
