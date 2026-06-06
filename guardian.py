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
DEFAULT_RESET_BUFFER = timedelta(minutes=2)

QUOTA_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\brate limited\b",
        r"\brate limits? reached\b",
        r"\brate limit exceeded\b",
        r"\busage limit reached\b",
        r"\bquota\s+(?:exceeded|reached|limit|limited|used up)\b",
        r"\b(?:exceeded|reached|hit)\s+(?:the\s+)?quota\b",
        r"\busage cap\b",
        r"\b(?:http\s*)?429\b.*\brate\b",
        r"try again in \d",
        r"retry after \d",
        r"额度已用尽",
        r"达到额度限制",
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


def get_task_by_source_key(source_key: str) -> dict[str, Any] | None:
    for task in load_tasks():
        if task.get("source_key") == source_key:
            return task
    return None


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


def remove_task(task_id: str) -> bool:
    tasks = load_tasks()
    kept = [task for task in tasks if task["id"] != task_id]
    if len(kept) == len(tasks):
        return False
    save_tasks(kept)
    return True


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

    if platform == "claude-app":
        return [
            "osascript",
            "-e",
            'tell application "Claude" to activate',
            "-e",
            "delay 1",
            "-e",
            (
                'tell application "System Events" to tell process "Claude" '
                'to click (first button of front window whose name contains "Keep working")'
            ),
        ]

    raise ValueError(f"unsupported platform: {platform}")


def is_quota_text(text: str) -> bool:
    return any(pattern.search(text) for pattern in QUOTA_PATTERNS)


def is_quota_signal_line(text: str) -> bool:
    if not is_quota_text(text):
        return False
    lowered = text.lower()
    ignored_contexts = (
        "如果遇到 quota/rate limit/usage limit",
        "quota detection is heuristic",
        "contains words such as",
        "is treated as rate-limited",
        "执行要求",
        "agent guardian 托管",
    )
    return not any(context in lowered for context in ignored_contexts)


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
    wall_time = extract_reset_wall_time(text, now=now)
    if wall_time is not None:
        return wall_time + DEFAULT_RESET_BUFFER
    duration = extract_duration_after_keyword(text)
    return now + (duration or DEFAULT_RETRY_DELAY)


RESET_TIME_RE = re.compile(
    r"\b(?:resets?|reset|available again|try again)\s*(?:at|around|by)?\s*"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?\b",
    re.IGNORECASE,
)

CHINESE_RESET_TIME_RE = re.compile(
    r"(?:重置|恢复|可用).*?(?P<period>凌晨|上午|中午|下午|晚上)?\s*(?P<hour>\d{1,2})(?:[:：](?P<minute>\d{2}))?"
)

CHINESE_RESET_TIME_REVERSE_RE = re.compile(
    r"(?P<period>凌晨|上午|中午|下午|晚上)?\s*(?P<hour>\d{1,2})(?:[:：](?P<minute>\d{2}))?.*?(?:重置|恢复|可用)"
)


def wall_time_candidate(
    hour: int,
    minute: int,
    ampm: str | None,
    now: datetime,
) -> datetime | None:
    if minute < 0 or minute > 59:
        return None
    if ampm:
        ampm = ampm.lower()
        if hour < 1 or hour > 12:
            return None
        if ampm == "am":
            hour = 0 if hour == 12 else hour
        elif ampm == "pm":
            hour = 12 if hour == 12 else hour + 12
    elif hour > 23:
        return None

    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def extract_reset_wall_time(text: str, *, now: datetime | None = None) -> datetime | None:
    now = now or local_now()
    match = RESET_TIME_RE.search(text)
    if match:
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        return wall_time_candidate(hour, minute, match.group("ampm"), now)

    match = CHINESE_RESET_TIME_RE.search(text)
    if not match:
        match = CHINESE_RESET_TIME_REVERSE_RE.search(text)
    if not match:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    period = match.group("period")
    if period in {"下午", "晚上"} and hour < 12:
        hour += 12
    if period in {"凌晨", "上午"} and hour == 12:
        hour = 0
    return wall_time_candidate(hour, minute, None, now)


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


def json_text(value: Any, *, limit: int = 20000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except TypeError:
        text = str(value)
    return text[-limit:]


def recent_files(root: Path, pattern: str, *, days: int, max_files: int = 200) -> list[Path]:
    if not root.exists():
        return []
    cutoff = time.time() - days * 86400
    files = [p for p in root.glob(pattern) if p.is_file() and p.stat().st_mtime >= cutoff]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]


def read_tail_text(path: Path, *, max_bytes: int = 1_000_000) -> str:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
        return fh.read().decode("utf-8", errors="ignore")


def datetime_from_epoch(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 10_000_000_000:
        number /= 1000
    return datetime.fromtimestamp(number, tz=local_now().tzinfo)


def register_discovered_task(
    *,
    platform: str,
    cwd: str,
    session: str,
    prompt: str,
    retry_at: datetime,
    source_key: str,
    source: str,
    auto_resume: bool,
    dry_run: bool,
) -> dict[str, Any]:
    retry_at_text = iso(retry_at)
    existing = get_task_by_source_key(source_key)
    if existing:
        existing.update(
            {
                "status": "rate_limited",
                "retry_at": retry_at_text,
                "auto_resume": auto_resume,
                "source": source,
                "prompt": existing.get("prompt") or prompt,
            }
        )
        existing["updated_at"] = iso(local_now())
        if not dry_run:
            upsert_task(existing)
            ensure_checkpoint(existing)
        return existing

    task = make_task(
        platform=platform,
        cwd=cwd,
        prompt=prompt,
        name=None,
        session=session,
        auto_resume=auto_resume,
        retry_at=retry_at_text,
    )
    task["source_key"] = source_key
    task["source"] = source
    task["status"] = "rate_limited"
    if not dry_run:
        upsert_task(task)
        ensure_checkpoint(task)
    return task


def discover_claude_cli(*, days: int, auto_resume: bool, dry_run: bool) -> list[dict[str, Any]]:
    discovered: list[dict[str, Any]] = []
    root = Path.home() / ".claude" / "projects"
    for path in recent_files(root, "*/*.jsonl", days=days, max_files=30):
        tail = read_tail_text(path, max_bytes=256_000)
        quota_lines = [line for line in tail.splitlines() if is_quota_signal_line(line)]
        if not quota_lines:
            continue

        session_id = path.stem
        cwd = str(Path.home())
        last_prompt = ""
        title = ""
        for raw in tail.splitlines():
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            session_id = obj.get("sessionId") or obj.get("session_id") or session_id
            cwd = obj.get("cwd") or cwd
            if obj.get("type") == "last-prompt":
                last_prompt = obj.get("lastPrompt") or last_prompt
            if obj.get("type") in {"ai-title", "custom-title"}:
                title = obj.get("aiTitle") or obj.get("customTitle") or title

        prompt = last_prompt or title or "Auto-discovered Claude Code quota-limited task"
        quota_context = "\n".join(quota_lines[-20:])
        retry_at = infer_retry_at(quota_context)
        discovered.append(
            register_discovered_task(
                platform="claude",
                cwd=cwd,
                session=session_id,
                prompt=prompt,
                retry_at=retry_at,
                source_key=f"claude-jsonl:{session_id}",
                source=str(path),
                auto_resume=auto_resume,
                dry_run=dry_run,
            )
        )
    return discovered


def codex_session_id_from_path(path: Path) -> str:
    match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        path.name,
        re.IGNORECASE,
    )
    return match.group(1) if match else path.stem


def discover_codex_sessions(
    *,
    days: int,
    auto_resume: bool,
    dry_run: bool,
    reached_threshold: float,
) -> list[dict[str, Any]]:
    discovered: list[dict[str, Any]] = []
    roots = [Path.home() / ".codex" / "sessions", Path.home() / ".codex" / "archived_sessions"]
    files: list[Path] = []
    for root in roots:
        files.extend(recent_files(root, "**/rollout-*.jsonl", days=days, max_files=30))

    for path in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
        session_id = codex_session_id_from_path(path)
        cwd = str(Path.home())
        prompt = "Auto-discovered Codex quota-limited task"
        latest_retry_at: datetime | None = None
        reached = False

        for raw in read_tail_text(path, max_bytes=512_000).splitlines():
            if (
                "rate_limits" not in raw
                and "session_meta" not in raw
                and '"user_message"' not in raw
            ):
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            payload = obj.get("payload") or {}
            if obj.get("type") == "session_meta":
                session_id = payload.get("id") or session_id
                cwd = payload.get("cwd") or cwd

            if obj.get("type") == "event_msg" and payload.get("type") == "user_message":
                message = payload.get("message")
                if isinstance(message, str) and message.strip():
                    prompt = message.strip()[:800]

            rate_limits = payload.get("rate_limits") if isinstance(payload, dict) else None
            if not isinstance(rate_limits, dict):
                continue

            if rate_limits.get("rate_limit_reached_type"):
                reached = True

            for bucket_name in ("primary", "secondary"):
                bucket = rate_limits.get(bucket_name)
                if not isinstance(bucket, dict):
                    continue
                used = bucket.get("used_percent")
                try:
                    used_float = float(used)
                except (TypeError, ValueError):
                    used_float = 0.0
                if used_float >= reached_threshold:
                    reached = True
                reset = datetime_from_epoch(bucket.get("resets_at"))
                if reset and reset > local_now():
                    if latest_retry_at is None or reset < latest_retry_at:
                        latest_retry_at = reset

        if not reached:
            continue
        discovered.append(
            register_discovered_task(
                platform="codex",
                cwd=cwd,
                session=session_id,
                prompt=prompt,
                retry_at=latest_retry_at or (local_now() + DEFAULT_RETRY_DELAY),
                source_key=f"codex-rollout:{session_id}",
                source=str(path),
                auto_resume=auto_resume,
                dry_run=dry_run,
            )
        )
    return discovered


def read_claude_app_text() -> tuple[str, str | None]:
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return "", "Claude App UI scanning is only supported on macOS with osascript."
    script = (
        'tell application "System Events" to tell process "Claude" '
        "to get value of every static text of every window"
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return "", result.stderr.strip() or result.stdout.strip()
    return result.stdout, None


def discover_claude_app_ui(
    *,
    auto_resume: bool,
    dry_run: bool,
    cwd: str,
) -> tuple[list[dict[str, Any]], str | None]:
    text, error = read_claude_app_text()
    if error:
        return [], error
    if not is_quota_text(text):
        return [], None

    retry_at = infer_retry_at(text)
    task = register_discovered_task(
        platform="claude-app",
        cwd=cwd,
        session="ui",
        prompt="Auto-discovered Claude App usage-limit task. Resume by clicking Keep working.",
        retry_at=retry_at,
        source_key="claude-app:front-window",
        source="Claude App front window",
        auto_resume=auto_resume,
        dry_run=dry_run,
    )
    return [task], None


def run_discovery(args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[str]]:
    platforms = {args.platform} if args.platform != "all" else {"claude", "codex"}
    warnings: list[str] = []
    auto_resume = parse_auto_resume(args.auto_resume)
    auto_resume_bool = True if auto_resume is None else auto_resume
    discovered: list[dict[str, Any]] = []

    if "claude" in platforms:
        discovered.extend(
            discover_claude_cli(
                days=args.days,
                auto_resume=auto_resume_bool,
                dry_run=args.dry_run,
            )
        )
    if "codex" in platforms:
        discovered.extend(
            discover_codex_sessions(
                days=args.days,
                auto_resume=auto_resume_bool,
                dry_run=args.dry_run,
                reached_threshold=args.codex_reached_threshold,
            )
        )
    if args.scan_ui:
        ui_tasks, error = discover_claude_app_ui(
            auto_resume=auto_resume_bool,
            dry_run=args.dry_run,
            cwd=args.cwd,
        )
        discovered.extend(ui_tasks)
        if error:
            warnings.append(error)
    return discovered, warnings


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


def cmd_update(args: argparse.Namespace) -> int:
    task = get_task(args.task_id)
    changes: dict[str, Any] = {}
    if args.retry_at:
        changes["retry_at"] = iso(parse_datetime(args.retry_at))
        changes["status"] = "rate_limited"
    if args.retry_after:
        changes["retry_at"] = iso(local_now() + parse_duration(args.retry_after))
        changes["status"] = "rate_limited"
    if args.auto_resume:
        changes["auto_resume"] = parse_auto_resume(args.auto_resume)
    if args.status:
        changes["status"] = args.status
    update_task(task, **changes)
    print(f"已更新任务: {task['id']}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    if remove_task(args.task_id):
        print(f"已删除任务: {args.task_id}")
        return 0
    raise SystemExit(f"task not found: {args.task_id}")


def cmd_discover(args: argparse.Namespace) -> int:
    discovered, warnings = run_discovery(args)
    if args.json:
        print(
            json.dumps(
                {"tasks": discovered, "warnings": warnings},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    for warning in warnings:
        print(f"警告: {warning}")
    if not discovered:
        print("未发现新的额度中断任务。")
        return 0
    for task in discovered:
        print(
            f"已发现/登记: {task['id']}  "
            f"{task['platform']}  session={task.get('session')}  "
            f"retry_at={task.get('retry_at')}"
        )
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
    if args.discover:
        discovered, warnings = run_discovery(args)
        for warning in warnings:
            print(f"发现扫描警告: {warning}")
        for task in discovered:
            print(f"自动发现额度中断任务: {task['id']} retry_at={task.get('retry_at')}")

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
    add.add_argument("--platform", choices=["claude", "codex", "claude-app"], required=True)
    add.add_argument("--cwd", default=os.getcwd())
    add.add_argument("--session", default="last")
    add.add_argument("--name")
    add.add_argument("--prompt")
    add.add_argument("--auto-resume", choices=["ask", "yes", "no"], default="yes")
    add.add_argument("--retry-after")
    add.add_argument("--retry-at")
    add.set_defaults(func=cmd_add)

    discover = sub.add_parser("discover", help="auto-discover quota-limited local sessions")
    discover.add_argument("--platform", choices=["all", "claude", "codex"], default="all")
    discover.add_argument("--days", type=int, default=2)
    discover.add_argument("--cwd", default=os.getcwd())
    discover.add_argument("--auto-resume", choices=["ask", "yes", "no"], default="yes")
    discover.add_argument("--codex-reached-threshold", type=float, default=100.0)
    discover.add_argument("--scan-ui", action="store_true", help="also scan Claude App window text")
    discover.add_argument("--dry-run", action="store_true")
    discover.add_argument("--json", action="store_true")
    discover.set_defaults(func=cmd_discover)

    list_cmd = sub.add_parser("list", help="list supervised tasks")
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="show one task as JSON")
    show.add_argument("task_id")
    show.set_defaults(func=cmd_show)

    update = sub.add_parser("update", help="update a registered task")
    update.add_argument("task_id")
    update.add_argument("--retry-after")
    update.add_argument("--retry-at")
    update.add_argument("--auto-resume", choices=["ask", "yes", "no"])
    update.add_argument("--status")
    update.set_defaults(func=cmd_update)

    delete = sub.add_parser("delete", help="delete a registered task")
    delete.add_argument("task_id")
    delete.set_defaults(func=cmd_delete)

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
    daemon.add_argument("--platform", choices=["all", "claude", "codex"], default="all")
    daemon.add_argument("--days", type=int, default=2)
    daemon.add_argument("--cwd", default=os.getcwd())
    daemon.add_argument("--auto-resume", choices=["ask", "yes", "no"], default="yes")
    daemon.add_argument("--codex-reached-threshold", type=float, default=100.0)
    daemon.add_argument("--scan-ui", action="store_true", help="also scan Claude App window text")
    daemon.add_argument("--dry-run", action="store_true")
    daemon.add_argument("--no-discover", dest="discover", action="store_false")
    daemon.set_defaults(discover=True)
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
