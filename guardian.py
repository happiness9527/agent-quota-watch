#!/usr/bin/env python3
"""Local continuity guardian for Codex and Claude Code tasks.

The script intentionally uses only public CLI resume commands and local state.
It does not read platform auth files or private quota databases.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import webbrowser


APP_NAME = "Agent Guardian"
DEFAULT_HOME = Path(
    os.environ.get("AGENT_GUARDIAN_HOME", Path.home() / ".agent-continuity")
).expanduser()
TASKS_FILE = DEFAULT_HOME / "tasks.json"
LOG_DIR = DEFAULT_HOME / "logs"
DEFAULT_RETRY_DELAY = timedelta(hours=5, minutes=10)
DEFAULT_WARN_AFTER = timedelta(hours=4, minutes=30)
DEFAULT_RESET_BUFFER = timedelta(minutes=2)
DEFAULT_QUOTA_WARNING_REMAINING_PERCENT = 10.0
DEFAULT_CODEX_NEAR_LIMIT_PERCENT = 100.0 - DEFAULT_QUOTA_WARNING_REMAINING_PERCENT
CLAUDE_DESKTOP_CACHE_DIR = (
    Path.home() / "Library" / "Application Support" / "Claude" / "Cache" / "Cache_Data"
)
CLAUDE_DESKTOP_QUOTA_CACHE = DEFAULT_HOME / "claude-desktop-quota.json"
CLAUDE_DESKTOP_QUOTA_FALLBACK_TTL = timedelta(minutes=30)

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
        r"\b(?:you['’]?ve\s+)?(?:hit|reached)\s+your\s+(?:\d+\s*-\s*hour\s+)?limit\b",
        r"\b\d+\s*-\s*hour\s+limit\b",
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


@dataclass
class QuotaStatus:
    platform: str
    label: str
    window: str
    used_percent: float | None
    remaining_percent: float | None
    resets_at: str | None
    source: str
    stale: bool = False
    error: str | None = None


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


CLAUDE_APP_SCRIPT_HELPERS = r'''
on elementText(elementRef)
  tell application "System Events"
    set pieces to {}
    try
      set elementName to name of elementRef as text
      if elementName is not "" then set end of pieces to elementName
    end try
    try
      set elementValue to value of elementRef as text
      if elementValue is not "" then set end of pieces to elementValue
    end try
    try
      set elementDescription to description of elementRef as text
      if elementDescription is not "" then set end of pieces to elementDescription
    end try
    return pieces as text
  end tell
end elementText

on findQuotaText(elementRef)
  tell application "System Events"
    set currentText to my elementText(elementRef)
    if (length of currentText) < 500 and (currentText contains "Usage limit" or currentText contains "usage limit" or currentText contains "5-hour limit" or currentText contains "resets" or currentText contains "reset" or currentText contains "Need more usage" or currentText contains "Get usage credits" or currentText contains "Wait until") then
      return currentText
    end if
    try
      set childElements to UI elements of elementRef
    on error
      set childElements to {}
    end try
    set childCount to count of childElements
    repeat with childIndex from childCount to 1 by -1
      set foundText to my findQuotaText(item childIndex of childElements)
      if foundText is not "" then return foundText
    end repeat
  end tell
  return ""
end findQuotaText
'''


CLAUDE_APP_QUOTA_TEXT_SCRIPT = (
    CLAUDE_APP_SCRIPT_HELPERS
    + r'''
tell application "Claude" to activate
delay 0.5
tell application "System Events" to tell process "Claude"
  return my findQuotaText(front window)
end tell
'''
)


CLAUDE_APP_RESUME_SCRIPT = (
    CLAUDE_APP_SCRIPT_HELPERS
    + r'''

on clickButtonByName(elementRef, targetName)
  tell application "System Events"
    try
      set elementRole to role of elementRef as text
    on error
      set elementRole to ""
    end try
    try
      set elementName to name of elementRef as text
    on error
      set elementName to ""
    end try
    if elementRole is "AXButton" and elementName contains targetName then
      click elementRef
      return true
    end if
    try
      set childElements to UI elements of elementRef
    on error
      set childElements to {}
    end try
    set childCount to count of childElements
    repeat with childIndex from childCount to 1 by -1
      if my clickButtonByName(item childIndex of childElements, targetName) then return true
    end repeat
  end tell
  return false
end clickButtonByName

on buttonExistsByName(elementRef, targetName)
  tell application "System Events"
    try
      set elementRole to role of elementRef as text
    on error
      set elementRole to ""
    end try
    try
      set elementName to name of elementRef as text
    on error
      set elementName to ""
    end try
    if elementRole is "AXButton" and elementName contains targetName then
      return true
    end if
    try
      set childElements to UI elements of elementRef
    on error
      set childElements to {}
    end try
    set childCount to count of childElements
    repeat with childIndex from childCount to 1 by -1
      if my buttonExistsByName(item childIndex of childElements, targetName) then return true
    end repeat
  end tell
  return false
end buttonExistsByName

on submitContinuationPrompt(elementRef, promptText)
  tell application "System Events"
    set currentText to my elementText(elementRef)
    try
      set elementRole to role of elementRef as text
    on error
      set elementRole to ""
    end try
    try
      set elementRoleDescription to role description of elementRef as text
    on error
      set elementRoleDescription to ""
    end try

    set isTextInput to false
    if elementRole contains "Text" or elementRole contains "text" then set isTextInput to true
    if elementRoleDescription contains "Text" or elementRoleDescription contains "text" or elementRoleDescription contains "文本" then set isTextInput to true

    set looksLikeComposer to false
    if currentText contains "Write a message" or currentText contains "Write your prompt" or currentText contains "Send a message" or currentText contains "message" or currentText contains "Message" or currentText contains "prompt" then set looksLikeComposer to true

    if isTextInput and looksLikeComposer then
      try
        set focused of elementRef to true
      end try
      click elementRef
      delay 0.2
      set previousClipboard to the clipboard
      set the clipboard to promptText
      delay 0.2
      keystroke "v" using command down
      delay 0.2
      key code 36
      delay 0.2
      set the clipboard to previousClipboard
      return true
    end if

    try
      set childElements to UI elements of elementRef
    on error
      set childElements to {}
    end try
    set childCount to count of childElements
    repeat with childIndex from childCount to 1 by -1
      if my submitContinuationPrompt(item childIndex of childElements, promptText) then return true
    end repeat
  end tell
  return false
end submitContinuationPrompt

tell application "Claude" to activate
delay 0.7
tell application "System Events" to tell process "Claude"
  try
    my clickButtonByName(front window, "Wait until")
    delay 0.7
  end try

  keystroke "r" using command down
  delay 4

  try
    my clickButtonByName(front window, "Wait until")
    delay 0.7
  end try

  if my buttonExistsByName(front window, "Get usage credits") or my buttonExistsByName(front window, "Wait until") then
    return my findQuotaText(front window)
  end if

  set continuationPrompt to "请继续完成刚才因为额度限制中断的任务。请先简要回顾上一步已经完成到哪里，然后直接继续完成剩余工作。"
  if my submitContinuationPrompt(front window, continuationPrompt) then
    delay 1.5
    if my buttonExistsByName(front window, "Get usage credits") or my buttonExistsByName(front window, "Wait until") then
      return my findQuotaText(front window)
    end if
    return "submitted Claude continuation prompt"
  end if

  set quotaText to my findQuotaText(front window)
  if quotaText is not "" then return quotaText
  error "Claude message input not found after quota reset"
end tell
'''
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
        return ["osascript", "-e", CLAUDE_APP_RESUME_SCRIPT]

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


def datetime_from_value(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return parse_datetime(text)
        except ValueError:
            return datetime_from_epoch(text)
    return datetime_from_epoch(value)


def percent_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        percent = float(value)
    except (TypeError, ValueError):
        return None
    if 0 < percent <= 1:
        percent *= 100
    return max(0.0, percent)


def quota_status(
    *,
    platform: str,
    label: str,
    window: str,
    used_percent: Any,
    resets_at: Any,
    source: str,
    stale: bool = False,
    error: str | None = None,
) -> QuotaStatus:
    used = percent_value(used_percent)
    remaining = None if used is None else max(0.0, 100.0 - used)
    reset_dt = datetime_from_value(resets_at)
    return QuotaStatus(
        platform=platform,
        label=label,
        window=window,
        used_percent=used,
        remaining_percent=remaining,
        resets_at=iso(reset_dt) if reset_dt else None,
        source=source,
        stale=stale,
        error=error,
    )


def parse_claude_desktop_usage_payload(payload: bytes | str) -> dict[str, Any]:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    data = json.loads(payload)
    five_hour = data.get("five_hour") or {}
    seven_day = data.get("seven_day") or {}
    return {
        "q5": percent_value(five_hour.get("utilization")),
        "q5_reset": iso(reset)
        if (reset := datetime_from_value(five_hour.get("resets_at")))
        else None,
        "q7": percent_value(seven_day.get("utilization")),
        "q7_reset": iso(reset)
        if (reset := datetime_from_value(seven_day.get("resets_at")))
        else None,
    }


def read_claude_desktop_usage_cache(
    cache_dir: Path = CLAUDE_DESKTOP_CACHE_DIR,
) -> tuple[dict[str, Any] | None, list[str], bool]:
    warnings: list[str] = []
    if sys.platform != "darwin":
        return None, ["Claude Desktop cache 读取仅支持 macOS。"], False
    if not cache_dir.exists():
        return None, [f"没有找到 Claude Desktop cache 目录：{cache_dir}"], False
    if not shutil.which("zstd"):
        return None, ["没有找到 zstd，无法读取 Claude Desktop /usage 缓存。可执行 brew install zstd。"], False

    candidate: tuple[float, bytes] | None = None
    for path in cache_dir.glob("*_0"):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"organizations/" not in data or b"/usage" not in data:
            continue
        magic_index = data.find(b"\x28\xb5\x2f\xfd")
        if magic_index < 0:
            continue
        mtime = path.stat().st_mtime
        if candidate is None or mtime > candidate[0]:
            candidate = (mtime, data[magic_index:])

    if candidate is None:
        return None, ["Claude Desktop cache 中暂未找到 /usage 响应。"], False

    result = subprocess.run(
        ["zstd", "-dc"],
        input=candidate[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    try:
        plan = parse_claude_desktop_usage_payload(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="ignore").strip()
            return None, [f"Claude Desktop /usage cache 解压失败：{detail or result.returncode}"], False
        return None, [f"Claude Desktop /usage cache 解析失败：{exc}"], False
    if plan.get("q5") is None and plan.get("q7") is None:
        return None, ["Claude Desktop /usage cache 未包含可用额度字段。"], False

    ensure_home()
    try:
        with CLAUDE_DESKTOP_QUOTA_CACHE.open("w", encoding="utf-8") as fh:
            json.dump({"saved_at": iso(local_now()), "plan": plan}, fh, ensure_ascii=False)
    except OSError:
        pass
    return plan, warnings, False


def read_claude_desktop_usage_with_fallback() -> tuple[dict[str, Any] | None, list[str], bool]:
    plan, warnings, stale = read_claude_desktop_usage_cache()
    if plan:
        return plan, warnings, stale
    try:
        with CLAUDE_DESKTOP_QUOTA_CACHE.open("r", encoding="utf-8") as fh:
            cached = json.load(fh)
        saved_at = parse_datetime(cached.get("saved_at", ""))
        if local_now() - saved_at <= CLAUDE_DESKTOP_QUOTA_FALLBACK_TTL:
            fallback = cached.get("plan")
            if isinstance(fallback, dict):
                return fallback, [*warnings, "Claude Desktop cache 本次未读到，暂用 30 分钟内的最后一次额度读数。"], True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None, warnings, False


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
    status: str = "rate_limited",
) -> dict[str, Any]:
    retry_at_text = iso(retry_at)
    existing = get_task_by_source_key(source_key)
    if existing:
        existing_prompt = existing.get("prompt") or ""
        should_refresh_prompt = existing_prompt.startswith("Auto-discovered")
        existing.update(
            {
                "status": status,
                "retry_at": retry_at_text,
                "auto_resume": auto_resume,
                "source": source,
                "prompt": prompt if should_refresh_prompt or not existing_prompt else existing_prompt,
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
    task["status"] = status
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


def latest_codex_rate_limits(days: int) -> tuple[dict[str, Any] | None, str | None]:
    roots = [Path.home() / ".codex" / "sessions", Path.home() / ".codex" / "archived_sessions"]
    files: list[Path] = []
    for root in roots:
        files.extend(recent_files(root, "**/rollout-*.jsonl", days=days, max_files=80))

    latest_codex: tuple[datetime, dict[str, Any], str] | None = None
    latest_any: tuple[datetime, dict[str, Any], str] | None = None
    for path in files:
        for raw in read_tail_text(path, max_bytes=512_000).splitlines():
            if "rate_limits" not in raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            payload = obj.get("payload") or {}
            rate_limits = payload.get("rate_limits") if isinstance(payload, dict) else None
            if not isinstance(rate_limits, dict):
                continue
            timestamp = datetime_from_value(obj.get("timestamp")) or datetime.fromtimestamp(
                path.stat().st_mtime,
                tz=local_now().tzinfo,
            )
            item = (timestamp, rate_limits, str(path))
            if latest_any is None or timestamp > latest_any[0]:
                latest_any = item
            if rate_limits.get("limit_id") == "codex" and (
                latest_codex is None or timestamp > latest_codex[0]
            ):
                latest_codex = item

    latest = latest_codex or latest_any
    if latest is None:
        return None, None
    return latest[1], latest[2]


def codex_quota_statuses(days: int) -> list[QuotaStatus]:
    rate_limits, source = latest_codex_rate_limits(days)
    if not rate_limits:
        return []
    primary = rate_limits.get("primary") or {}
    secondary = rate_limits.get("secondary") or {}
    source_text = source or "Codex rollout rate_limits"
    statuses: list[QuotaStatus] = []
    if primary:
        statuses.append(
            quota_status(
                platform="codex",
                label="Codex 5h",
                window="5h",
                used_percent=primary.get("used_percent"),
                resets_at=primary.get("resets_at"),
                source=source_text,
            )
        )
    if secondary:
        statuses.append(
            quota_status(
                platform="codex",
                label="Codex 周",
                window="week",
                used_percent=secondary.get("used_percent"),
                resets_at=secondary.get("resets_at"),
                source=source_text,
            )
        )
    return statuses


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
        reached_limit = False
        max_used_percent = 0.0

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
                reached_limit = True

            for bucket_name in ("primary", "secondary"):
                bucket = rate_limits.get(bucket_name)
                if not isinstance(bucket, dict):
                    continue
                used = bucket.get("used_percent")
                try:
                    used_float = float(used)
                except (TypeError, ValueError):
                    used_float = 0.0
                max_used_percent = max(max_used_percent, used_float)
                if used_float >= reached_threshold:
                    reached = True
                if used_float >= 100.0:
                    reached_limit = True
                reset = datetime_from_value(bucket.get("resets_at"))
                if reset and reset > local_now():
                    reset = reset + DEFAULT_RESET_BUFFER
                    if latest_retry_at is None or reset < latest_retry_at:
                        latest_retry_at = reset

        if not reached:
            continue
        status = "rate_limited" if reached_limit else "scheduled"
        prompt = (
            f"{prompt}\n\n"
            f"[Guardian] Codex 使用量已接近阈值：最高 used_percent={max_used_percent:.1f}%，"
            f"将在额度窗口重置后自动续跑。"
        )
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
                status=status,
            )
        )
    return discovered


def claude_desktop_quota_statuses() -> tuple[list[QuotaStatus], list[str]]:
    plan, warnings, stale = read_claude_desktop_usage_with_fallback()
    if not plan:
        return [], warnings
    statuses: list[QuotaStatus] = []
    if plan.get("q5") is not None or plan.get("q5_reset"):
        statuses.append(
            quota_status(
                platform="claude-app",
                label="Claude 5h",
                window="5h",
                used_percent=plan.get("q5"),
                resets_at=plan.get("q5_reset"),
                source="Claude Desktop /usage cache",
                stale=stale,
            )
        )
    if plan.get("q7") is not None or plan.get("q7_reset"):
        statuses.append(
            quota_status(
                platform="claude-app",
                label="Claude 周",
                window="week",
                used_percent=plan.get("q7"),
                resets_at=plan.get("q7_reset"),
                source="Claude Desktop /usage cache",
                stale=stale,
            )
        )
    return statuses, warnings


def collect_quota_statuses(
    *,
    days: int,
    include_claude_app: bool,
) -> tuple[list[QuotaStatus], list[str]]:
    statuses = codex_quota_statuses(days)
    warnings: list[str] = []
    if include_claude_app:
        claude_statuses, claude_warnings = claude_desktop_quota_statuses()
        statuses.extend(claude_statuses)
        warnings.extend(claude_warnings)
    return statuses, warnings


def discover_claude_app_quota_from_statuses(
    statuses: list[QuotaStatus],
    *,
    auto_resume: bool,
    dry_run: bool,
    cwd: str,
    reached_threshold: float,
) -> list[dict[str, Any]]:
    five_hour = next((status for status in statuses if status.window == "5h"), None)
    if not five_hour or five_hour.used_percent is None:
        return []
    if five_hour.used_percent < reached_threshold:
        return []

    retry_at = (
        parse_datetime(five_hour.resets_at) + DEFAULT_RESET_BUFFER
        if five_hour.resets_at
        else local_now() + DEFAULT_RETRY_DELAY
    )
    status = "rate_limited" if five_hour.used_percent >= 100.0 else "scheduled"
    remaining = five_hour.remaining_percent if five_hour.remaining_percent is not None else 0.0
    prompt = (
        "Auto-discovered Claude App usage-limit task. "
        f"Claude Desktop 5h 剩余约 {remaining:.1f}%，到重置后自动发送继续任务提示。"
    )
    task = register_discovered_task(
        platform="claude-app",
        cwd=cwd,
        session="ui",
        prompt=prompt,
        retry_at=retry_at,
        source_key="claude-app:front-window",
        source="Claude Desktop /usage cache",
        auto_resume=auto_resume,
        dry_run=dry_run,
        status=status,
    )
    return [task]


def discover_claude_app_quota(
    *,
    auto_resume: bool,
    dry_run: bool,
    cwd: str,
    reached_threshold: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    statuses, warnings = claude_desktop_quota_statuses()
    return (
        discover_claude_app_quota_from_statuses(
            statuses,
            auto_resume=auto_resume,
            dry_run=dry_run,
            cwd=cwd,
            reached_threshold=reached_threshold,
        ),
        warnings,
    )


def read_claude_app_text() -> tuple[str, str | None]:
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return "", "Claude App UI scanning is only supported on macOS with osascript."
    try:
        result = subprocess.run(
            ["osascript", "-e", CLAUDE_APP_QUOTA_TEXT_SCRIPT],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return "", "读取 Claude 桌面 App 窗口超时。请确认 Claude 窗口没有卡住，并保持窗口可见。"
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
    if not text.strip():
        return [], (
            "已连接 Claude 桌面 App，但没有读取到窗口文字。"
            "请确认 Claude 窗口处于打开状态；若仍为空，说明当前版本的 Claude 桌面 App "
            "没有把这段提示暴露给 macOS 辅助功能。可在下方手动粘贴 Usage limit 文案登记。"
        )
    if not is_quota_text(text):
        return [], None

    retry_at = infer_retry_at(text)
    task = register_discovered_task(
        platform="claude-app",
        cwd=cwd,
        session="ui",
        prompt="Auto-discovered Claude App usage-limit task. Resume by submitting a continuation prompt after reset.",
        retry_at=retry_at,
        source_key="claude-app:front-window",
        source="Claude App front window",
        auto_resume=auto_resume,
        dry_run=dry_run,
    )
    return [task], None


def register_claude_app_limit_text(
    text: str,
    *,
    cwd: str,
    auto_resume: bool,
    dry_run: bool,
) -> dict[str, Any]:
    if not text.strip():
        raise ValueError("请先粘贴 Claude 桌面 App 的额度提示文案。")
    if not is_quota_text(text):
        raise ValueError("没有识别到额度中断文案。请粘贴包含 Usage limit reached / Resets 2:30 AM 的完整提示。")
    retry_at = infer_retry_at(text)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return register_discovered_task(
        platform="claude-app",
        cwd=cwd,
        session="ui",
        prompt=f"Claude 桌面 App 手动登记: {short_text(text, limit=240)}",
        retry_at=retry_at,
        source_key=f"claude-app:manual:{digest}",
        source="manual Claude App usage-limit text",
        auto_resume=auto_resume,
        dry_run=dry_run,
    )


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
        quota_statuses, quota_warnings = claude_desktop_quota_statuses()
        quota_tasks = discover_claude_app_quota_from_statuses(
            quota_statuses,
            auto_resume=auto_resume_bool,
            dry_run=args.dry_run,
            cwd=args.cwd,
            reached_threshold=100.0 - args.quota_warning_remaining,
        )
        discovered.extend(quota_tasks)
        warnings.extend(quota_warnings)
        has_cache_quota = any(
            status.platform == "claude-app"
            and status.window == "5h"
            and status.used_percent is not None
            and not status.stale
            for status in quota_statuses
        )
        if not has_cache_quota:
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


def pending_tasks(tasks: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    tasks = tasks if tasks is not None else load_tasks()
    return [task for task in tasks if task.get("status") in {"rate_limited", "scheduled"}]


def next_retry_task(tasks: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    candidates = []
    for task in pending_tasks(tasks):
        retry_at = task.get("retry_at")
        if not retry_at:
            candidates.append((local_now(), task))
            continue
        try:
            candidates.append((parse_datetime(retry_at), task))
        except ValueError:
            continue
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[0][1]


def daemon_tick(args: argparse.Namespace, *, quiet: bool = False) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "scanned_at": iso(local_now()),
        "discovered": [],
        "warnings": [],
        "resumed": [],
    }
    if args.discover:
        discovered, warnings = run_discovery(args)
        summary["discovered"] = discovered
        summary["warnings"] = warnings
        for warning in warnings:
            if not quiet:
                print(f"发现扫描警告: {warning}")
        for task in discovered:
            if not quiet:
                print(f"自动发现额度中断任务: {task['id']} retry_at={task.get('retry_at')}")

    for task in load_tasks():
        if not due_for_resume(task):
            continue
        if not quiet:
            print(f"开始恢复任务: {task['id']}")
        result = execute_task(
            task,
            resume=True,
            warn_after=parse_duration(args.warn_after),
            no_popup=args.no_popup,
        )
        summary["resumed"].append(
            {"id": task["id"], "status": result.status, "retry_at": result.retry_at}
        )
        if result.status == "rate_limited":
            if not quiet:
                print(f"任务仍受额度限制，已重新排队: {task['id']} -> {result.retry_at}")
        else:
            if not quiet:
                print(f"任务恢复结束: {task['id']} status={result.status}")
    return summary


def cmd_daemon(args: argparse.Namespace) -> int:
    print(f"{APP_NAME} 守护进程已启动")
    print(f"状态文件: {TASKS_FILE}")
    print(f"扫描范围: Claude Code / Codex{' / Claude 桌面 App' if args.scan_ui else ''}")
    print("说明: 没有新输出不代表失败；每次扫描完成后会打印中文状态。")
    while True:
        summary = daemon_tick(args)
        tasks = load_tasks()
        pending = pending_tasks(tasks)
        next_task = next_retry_task(tasks)
        print(
            f"[{summary['scanned_at']}] 扫描完成: "
            f"新发现 {len(summary['discovered'])} 个，"
            f"已恢复 {len(summary['resumed'])} 个，"
            f"等待恢复 {len(pending)} 个。"
        )
        if next_task:
            print(f"下一次预计恢复: {next_task.get('retry_at')}  task={next_task.get('id')}")
        if args.once:
            return 0
        print(f"下次扫描将在 {args.interval} 秒后执行。按 Ctrl+C 停止。")
        time.sleep(args.interval)


STATUS_LABELS = {
    "created": "已创建",
    "scheduled": "等待重置",
    "running": "运行中",
    "resuming": "恢复中",
    "rate_limited": "额度中断",
    "completed": "已完成",
    "failed": "失败",
}

PLATFORM_LABELS = {
    "claude": "Claude Code",
    "claude-app": "Claude 桌面 App",
    "codex": "Codex",
}


def resume_time_reached(task: dict[str, Any]) -> bool:
    retry_at = task.get("retry_at")
    if not retry_at:
        return True
    try:
        return parse_datetime(retry_at) <= local_now()
    except ValueError:
        return True


def can_resume_manually(task: dict[str, Any]) -> bool:
    status = task.get("status")
    if status == "failed":
        return True
    if status in {"rate_limited", "scheduled"}:
        return resume_time_reached(task)
    return False


def h(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def short_text(value: Any, *, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def resume_action_description(task: dict[str, Any]) -> str:
    platform = task.get("platform")
    if platform == "claude-app":
        return "刷新 Claude 桌面 App，确认额度提示消失后，在当前对话自动发送继续任务提示。"
    return " ".join(build_command(task, resume=True))


def format_percent(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}%"


def format_reset(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return parse_datetime(value).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return value


def quota_level(status: QuotaStatus, *, warning_remaining: float) -> str:
    if status.error or status.remaining_percent is None:
        return "muted"
    if status.remaining_percent <= 0:
        return "danger"
    if status.remaining_percent <= warning_remaining:
        return "warn"
    return "ok"


def quota_level_label(status: QuotaStatus, *, warning_remaining: float) -> str:
    level = quota_level(status, warning_remaining=warning_remaining)
    if level == "danger":
        return "已用尽"
    if level == "warn":
        return "接近上限"
    if level == "ok":
        return "正常"
    return "未知"


def render_quota_cards_html(
    statuses: list[QuotaStatus],
    warnings: list[str],
    *,
    warning_remaining: float,
) -> str:
    cards: list[str] = []
    for status in statuses:
        level = quota_level(status, warning_remaining=warning_remaining)
        stale = " · 缓存值" if status.stale else ""
        cards.append(
            f"""
            <div class="card quota-card quota-{h(level)}">
              <div class="label">{h(status.label)}</div>
              <div class="value">{h(format_percent(status.remaining_percent))}</div>
              <div class="quota-meta">已用 {h(format_percent(status.used_percent))} · 重置 {h(format_reset(status.resets_at))}</div>
              <div class="quota-meta">{h(quota_level_label(status, warning_remaining=warning_remaining))}{h(stale)}</div>
              <div class="meter"><span style="width:{h(min(max(status.used_percent or 0.0, 0.0), 100.0))}%"></span></div>
              <div class="quota-source">{h(short_text(status.source, limit=96))}</div>
            </div>
            """
        )

    for warning in warnings[:2]:
        cards.append(
            f"""
            <div class="card quota-card quota-muted">
              <div class="label">额度采集提示</div>
              <div class="value">-</div>
              <div class="quota-meta">{h(short_text(warning, limit=180))}</div>
            </div>
            """
        )

    if not cards:
        cards.append(
            """
            <div class="card quota-card quota-muted">
              <div class="label">额度状态</div>
              <div class="value">-</div>
              <div class="quota-meta">暂无 Codex / Claude 桌面 App 额度数据。保持页面运行，下一轮扫描会继续尝试。</div>
            </div>
            """
        )
    return "\n".join(cards)


def dashboard_daemon_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        discover=True,
        platform=args.platform,
        days=args.days,
        cwd=args.cwd,
        auto_resume="yes",
        codex_reached_threshold=args.codex_reached_threshold,
        quota_warning_remaining=args.quota_warning_remaining,
        scan_ui=args.scan_ui,
        dry_run=False,
        warn_after="4h30m",
        no_popup=True,
    )


def render_dashboard_html(args: argparse.Namespace, worker_state: dict[str, Any]) -> str:
    tasks = load_tasks()
    pending = pending_tasks(tasks)
    next_task = next_retry_task(tasks)
    warnings = worker_state.get("warnings") or []
    last_scan = worker_state.get("last_scan") or "尚未扫描"
    last_action = worker_state.get("last_action") or "暂无操作。点击按钮后，这里会显示扫描、登记或恢复结果。"
    next_scan_at = worker_state.get("next_scan_at") or "即将执行"
    worker_enabled = not args.no_worker
    quota_statuses, quota_warnings = collect_quota_statuses(
        days=args.days,
        include_claude_app=args.scan_ui,
    )
    quota_cards_html = render_quota_cards_html(
        quota_statuses,
        quota_warnings,
        warning_remaining=args.quota_warning_remaining,
    )
    claude_ui_note = (
        "已开启 Claude 桌面 App 监控。优先读取本地 /usage cache 获取剩余额度；窗口文字扫描作为兜底。"
        if args.scan_ui
        else "默认只扫描 Claude Code/Codex 本地会话。要扫描 Claude 桌面 App，请用 dashboard --scan-ui 启动。"
    )

    rows = []
    for task in sorted(tasks, key=lambda item: item.get("updated_at") or "", reverse=True):
        status = STATUS_LABELS.get(task.get("status"), task.get("status") or "-")
        platform = PLATFORM_LABELS.get(task.get("platform"), task.get("platform") or "-")
        retry_at = task.get("retry_at") or "-"
        prompt = short_text(task.get("prompt") or task.get("name") or task.get("id"))
        if can_resume_manually(task):
            action_html = (
                f'<form method="post" action="/action/resume">'
                f'<input type="hidden" name="id" value="{h(task.get("id"))}">'
                f'<button>立即恢复</button></form>'
            )
        elif task.get("status") in {"rate_limited", "scheduled"} and task.get("auto_resume"):
            action_html = '<span class="muted">到点自动恢复</span>'
        elif task.get("status") in {"rate_limited", "scheduled"}:
            action_html = '<span class="muted">等待手动处理</span>'
        elif task.get("status") in {"running", "resuming"}:
            action_html = '<span class="muted">执行中</span>'
        else:
            action_html = '<span class="muted">无需恢复</span>'
        row = f"""
        <tr>
          <td><span class="pill status-{h(task.get('status'))}">{h(status)}</span></td>
          <td>{h(platform)}</td>
          <td>
            <div class="task-id">{h(task.get('id'))}</div>
            <div class="muted">{h(prompt)}</div>
          </td>
          <td>{h(retry_at)}</td>
          <td>{h(task.get('cwd'))}</td>
          <td class="actions">
            {action_html}
            <form method="post" action="/action/delete"><input type="hidden" name="id" value="{h(task.get('id'))}"><button class="danger">删除</button></form>
          </td>
        </tr>
        """
        rows.append(row)

    warnings_html = "".join(f"<li>{h(warning)}</li>" for warning in warnings)
    if not warnings_html:
        warnings_html = "<li>暂无警告。</li>"

    task_rows = "\n".join(rows) or """
      <tr><td colspan="6" class="empty">暂无托管任务。保持页面打开，Guardian 会继续扫描。</td></tr>
    """

    plan_items = []
    for task in pending:
        action_preview = resume_action_description(task)
        plan_items.append(
            f"<li><strong>{h(task.get('retry_at') or '到期后立即')}</strong> "
            f"{h(PLATFORM_LABELS.get(task.get('platform'), task.get('platform')))} "
            f"<span class='muted'>{h(short_text(task.get('id'), limit=80))}</span>"
            f"<br><code>{h(short_text(action_preview, limit=260))}</code></li>"
        )
    if not plan_items:
        plan_items.append("<li>当前没有自动恢复计划。原因：没有发现处于额度中断/等待恢复状态的任务。</li>")
    plan_html = "".join(plan_items)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="20">
  <title>Agent Continuity Guardian</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #17202a;
      --muted: #667085;
      --border: #d9dee7;
      --accent: #1769aa;
      --danger: #b42318;
      --ok: #067647;
      --warn: #b54708;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #101418;
        --panel: #171d23;
        --text: #edf2f7;
        --muted: #aab4c0;
        --border: #2d3742;
        --accent: #7cc4ff;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      padding: 28px 32px 18px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
    }}
    h1 {{ margin: 0 0 8px; font-size: 26px; letter-spacing: 0; }}
    p {{ margin: 0; color: var(--muted); line-height: 1.55; }}
    main {{ padding: 24px 32px 40px; max-width: 1400px; margin: 0 auto; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-bottom: 18px; }}
    .quota-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-bottom: 18px; }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
    }}
    .label {{ color: var(--muted); font-size: 13px; margin-bottom: 6px; }}
    .value {{ font-size: 22px; font-weight: 700; }}
    .toolbar {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 18px 0; }}
    button, .button {{
      appearance: none;
      border: 1px solid var(--border);
      background: var(--panel);
      color: var(--text);
      padding: 9px 12px;
      border-radius: 7px;
      cursor: pointer;
      font-size: 14px;
    }}
    button.primary, .button.primary {{ background: var(--accent); border-color: var(--accent); color: white; }}
    button.danger {{ color: var(--danger); }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
    th, td {{ padding: 12px; border-bottom: 1px solid var(--border); vertical-align: top; text-align: left; font-size: 14px; }}
    th {{ color: var(--muted); font-weight: 600; white-space: nowrap; }}
    .task-id {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; margin-bottom: 4px; }}
    .muted {{ color: var(--muted); }}
    .pill {{ display: inline-block; padding: 4px 8px; border-radius: 999px; background: #eef2f6; color: #344054; white-space: nowrap; }}
    .status-completed {{ color: var(--ok); }}
    .status-rate_limited, .status-scheduled {{ color: var(--warn); }}
    .status-failed {{ color: var(--danger); }}
    .quota-card {{ min-height: 150px; }}
    .quota-ok .value {{ color: var(--ok); }}
    .quota-warn .value {{ color: var(--warn); }}
    .quota-danger .value {{ color: var(--danger); }}
    .quota-meta {{ color: var(--muted); font-size: 13px; line-height: 1.45; margin-top: 6px; }}
    .quota-source {{ color: var(--muted); font-size: 12px; line-height: 1.35; margin-top: 8px; }}
    .meter {{ height: 6px; background: #e7ecf2; border-radius: 999px; overflow: hidden; margin-top: 10px; }}
    .meter span {{ display: block; height: 100%; background: currentColor; }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .actions form {{ margin: 0; }}
    .empty {{ text-align: center; color: var(--muted); padding: 28px; }}
    .notice {{ margin: 18px 0; display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
    ul {{ margin: 8px 0 0 18px; padding: 0; color: var(--muted); }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .manual-form {{ display: grid; gap: 10px; margin-top: 12px; }}
    textarea {{
      width: 100%;
      min-height: 82px;
      border: 1px solid var(--border);
      border-radius: 7px;
      background: var(--panel);
      color: var(--text);
      padding: 10px;
      font: inherit;
      resize: vertical;
    }}
    @media (max-width: 900px) {{
      header, main {{ padding-left: 16px; padding-right: 16px; }}
      .grid, .quota-grid, .notice {{ grid-template-columns: 1fr; }}
      table {{ display: block; overflow-x: auto; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Agent Continuity Guardian</h1>
    <p>本地守护页面：自动扫描 Codex 与 Claude 的额度中断任务，到恢复时间后自动继续。</p>
  </header>
  <main>
    <section class="grid">
      <div class="card"><div class="label">守护状态</div><div class="value">{'运行中' if worker_enabled else '手动模式'}</div></div>
      <div class="card"><div class="label">托管任务</div><div class="value">{len(tasks)}</div></div>
      <div class="card"><div class="label">等待恢复</div><div class="value">{len(pending)}</div></div>
      <div class="card"><div class="label">下一次恢复</div><div class="value">{h(next_task.get('retry_at') if next_task else '-')}</div></div>
    </section>

    <section class="quota-grid">
      {quota_cards_html}
    </section>

    <section class="toolbar">
      <form method="post" action="/action/discover"><button class="primary">立即扫描 Codex / Claude Code</button></form>
      <form method="post" action="/action/discover-ui"><button>扫描 Claude 桌面 App</button></form>
      <form method="post" action="/action/tick"><button>恢复到期任务</button></form>
      <a class="button" href="/">刷新页面</a>
    </section>

    <section class="notice">
      <div class="card">
        <div class="label">最近扫描</div>
        <p>{h(last_scan)}</p>
        <p class="muted">Dashboard 每 {args.interval} 秒后台扫描一次；下一次后台扫描：{h(next_scan_at)}。页面每 20 秒自动刷新。</p>
      </div>
      <div class="card">
        <div class="label">Claude 桌面 App 说明</div>
        <p>{h(claude_ui_note)}</p>
        <ul>{warnings_html}</ul>
      </div>
    </section>

    <section class="notice">
      <div class="card">
        <div class="label">最近操作结果</div>
        <p>{h(last_action)}</p>
      </div>
      <div class="card">
        <div class="label">自动恢复计划</div>
        <ul>{plan_html}</ul>
        <p class="muted">确认方式：这里出现具体时间和动作后，后台 worker 会每 {args.interval} 秒检查一次；到点后自动执行对应恢复动作。</p>
      </div>
    </section>

    <section class="card" style="margin-bottom:18px">
      <div class="label">Claude 桌面 App 兜底登记</div>
      <p class="muted">如果自动扫描读不到 Claude 窗口文字，把底部提示整句粘贴到这里，例如：Usage limit reached • Resets 2:30 AM • Keep working。</p>
      <form method="post" action="/action/register-claude-app-text" class="manual-form">
        <textarea name="limit_text" placeholder="粘贴 Claude 桌面 App 的额度提示文案"></textarea>
        <button class="primary">解析并登记自动恢复</button>
      </form>
    </section>

    <table>
      <thead>
        <tr><th>状态</th><th>平台</th><th>任务</th><th>恢复时间</th><th>目录</th><th>操作</th></tr>
      </thead>
      <tbody>{task_rows}</tbody>
    </table>

    <section class="card" style="margin-top:18px">
      <div class="label">推荐启动方式</div>
      <p><code>python3 guardian.py dashboard --scan-ui --open --keep-awake</code></p>
      <p class="muted">如果只监控 Codex / Claude Code 本地会话，也可以使用 <code>python3 guardian.py dashboard --open</code>。监控 Claude 桌面 App 前，先给 Terminal/Python 开辅助功能权限。</p>
    </section>
  </main>
</body>
</html>"""


def cmd_dashboard(args: argparse.Namespace) -> int:
    daemon_args = dashboard_daemon_args(args)
    lock = threading.Lock()
    stop_event = threading.Event()
    keep_awake_process: subprocess.Popen[str] | None = None
    worker_state: dict[str, Any] = {
        "last_scan": "尚未扫描",
        "warnings": [],
        "last_action": "暂无操作。点击按钮后，这里会显示扫描、登记或恢复结果。",
        "next_scan_at": "即将执行",
    }

    def run_tick(*, scan_ui_override: bool | None = None) -> dict[str, Any]:
        local_args = argparse.Namespace(**vars(daemon_args))
        if scan_ui_override is not None:
            local_args.scan_ui = scan_ui_override
        with lock:
            summary = daemon_tick(local_args, quiet=True)
            worker_state["last_scan"] = summary["scanned_at"]
            worker_state["warnings"] = summary.get("warnings") or []
            worker_state["last_summary"] = summary
            worker_state["next_scan_at"] = iso(local_now() + timedelta(seconds=args.interval))
            return summary

    def worker() -> None:
        while not stop_event.is_set():
            run_tick()
            stop_event.wait(args.interval)

    if not args.no_worker:
        threading.Thread(target=worker, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *values: Any) -> None:
            if args.verbose:
                super().log_message(format, *values)

        def send_html(self, body: str, *, status: int = 200) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def redirect_home(self) -> None:
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/":
                self.send_html("<h1>Not Found</h1>", status=404)
                return
            self.send_html(render_dashboard_html(args, worker_state))

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", errors="ignore")
            form = parse_qs(body)
            task_id = (form.get("id") or [""])[0]

            try:
                if parsed.path == "/action/discover":
                    summary = run_tick(scan_ui_override=False)
                    worker_state["last_action"] = (
                        f"Codex / Claude Code 扫描完成：新发现 {len(summary['discovered'])} 个，"
                        f"恢复 {len(summary['resumed'])} 个，警告 {len(summary['warnings'])} 条。"
                    )
                elif parsed.path == "/action/discover-ui":
                    summary = run_tick(scan_ui_override=True)
                    worker_state["last_action"] = (
                        f"Claude 桌面 App 扫描完成：新发现 {len(summary['discovered'])} 个，"
                        f"恢复 {len(summary['resumed'])} 个，警告 {len(summary['warnings'])} 条。"
                    )
                elif parsed.path == "/action/tick":
                    summary = run_tick()
                    worker_state["last_action"] = (
                        f"到期任务检查完成：恢复 {len(summary['resumed'])} 个，"
                        f"当前等待恢复 {len(pending_tasks())} 个。"
                    )
                elif parsed.path == "/action/delete" and task_id:
                    remove_task(task_id)
                    worker_state["last_action"] = f"已删除任务：{task_id}"
                elif parsed.path == "/action/register-claude-app-text":
                    limit_text = (form.get("limit_text") or [""])[0]
                    task = register_claude_app_limit_text(
                        limit_text,
                        cwd=args.cwd,
                        auto_resume=True,
                        dry_run=False,
                    )
                    worker_state["last_action"] = (
                        f"已登记 Claude 桌面 App 自动恢复任务：{task['id']}，"
                        f"恢复时间 {task.get('retry_at')}。"
                    )
                elif parsed.path == "/action/resume" and task_id:
                    task = get_task(task_id)
                    if not can_resume_manually(task):
                        worker_state["last_action"] = (
                            f"任务 {task_id} 当前状态是 "
                            f"{STATUS_LABELS.get(task.get('status'), task.get('status'))}，无需恢复。"
                        )
                        self.redirect_home()
                        return
                    result = execute_task(
                        task,
                        resume=True,
                        warn_after=parse_duration("4h30m"),
                        no_popup=True,
                    )
                    if result.status == "rate_limited":
                        worker_state["last_action"] = (
                            f"任务仍受额度限制，已重新排队：{task_id}，"
                            f"恢复时间 {result.retry_at or get_task(task_id).get('retry_at')}。"
                        )
                    elif result.status == "failed":
                        worker_state["last_action"] = (
                            f"立即恢复失败：{task_id}，请查看日志 {task.get('log_path')}。"
                        )
                    else:
                        worker_state["last_action"] = f"已执行立即恢复：{task_id}，状态 {result.status}。"
                else:
                    self.send_html("<h1>Bad Request</h1>", status=400)
                    return
            except Exception as exc:  # noqa: BLE001 - local dashboard should surface errors.
                self.send_html(
                    f"<h1>操作失败</h1><p>{h(exc)}</p><p><a href='/'>返回</a></p>",
                    status=500,
                )
                return
            self.redirect_home()

    url = f"http://{args.host}:{args.port}"
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print("Agent Guardian 可视化页面已经在运行。")
            print(f"已有页面地址: {url}")
            print("不用重复启动，直接打开上面的地址即可。")
            print(f"如果你想另开一个页面，可以换端口: python3 guardian.py dashboard --port {args.port + 1} --open")
            if args.open:
                webbrowser.open(url)
            return 0
        raise
    print(f"Agent Guardian 可视化页面已启动: {url}", flush=True)
    print("保持这个终端窗口打开，页面和后台扫描才会继续运行。按 Ctrl+C 停止。", flush=True)
    if args.keep_awake:
        if sys.platform == "darwin" and shutil.which("caffeinate"):
            keep_awake_process = subprocess.Popen(
                ["caffeinate", "-disu", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            print("已开启 macOS 防休眠：Dashboard 运行期间会尽量保持唤醒。", flush=True)
        else:
            print("未开启防休眠：当前系统没有可用的 caffeinate。", flush=True)
    if args.scan_ui:
        print(
            "已开启 Claude 桌面 App 扫描；如果页面显示权限警告，请到 macOS 设置开启辅助功能权限。",
            flush=True,
        )
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止 Dashboard。")
    finally:
        stop_event.set()
        if keep_awake_process and keep_awake_process.poll() is None:
            keep_awake_process.terminate()
        server.server_close()
    return 0


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
    discover.add_argument("--codex-reached-threshold", type=float, default=DEFAULT_CODEX_NEAR_LIMIT_PERCENT)
    discover.add_argument(
        "--quota-warning-remaining",
        type=float,
        default=DEFAULT_QUOTA_WARNING_REMAINING_PERCENT,
        help="register Claude App auto-resume when remaining 5h quota is at or below this percent",
    )
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
    daemon.add_argument("--codex-reached-threshold", type=float, default=DEFAULT_CODEX_NEAR_LIMIT_PERCENT)
    daemon.add_argument(
        "--quota-warning-remaining",
        type=float,
        default=DEFAULT_QUOTA_WARNING_REMAINING_PERCENT,
        help="register Claude App auto-resume when remaining 5h quota is at or below this percent",
    )
    daemon.add_argument("--scan-ui", action="store_true", help="also scan Claude App window text")
    daemon.add_argument("--dry-run", action="store_true")
    daemon.add_argument("--no-discover", dest="discover", action="store_false")
    daemon.set_defaults(discover=True)
    daemon.set_defaults(func=cmd_daemon)

    dashboard = sub.add_parser("dashboard", help="start a local Chinese web dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--interval", type=int, default=30)
    dashboard.add_argument("--platform", choices=["all", "claude", "codex"], default="all")
    dashboard.add_argument("--days", type=int, default=2)
    dashboard.add_argument("--cwd", default=os.getcwd())
    dashboard.add_argument("--codex-reached-threshold", type=float, default=DEFAULT_CODEX_NEAR_LIMIT_PERCENT)
    dashboard.add_argument(
        "--quota-warning-remaining",
        type=float,
        default=DEFAULT_QUOTA_WARNING_REMAINING_PERCENT,
        help="show low-quota warning when remaining quota is at or below this percent",
    )
    dashboard.add_argument("--scan-ui", action="store_true", help="also scan Claude App window text")
    dashboard.add_argument("--no-worker", action="store_true", help="serve the page without background scanning")
    dashboard.add_argument("--keep-awake", action="store_true", help="keep macOS awake while dashboard is running")
    dashboard.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    dashboard.add_argument("--verbose", action="store_true")
    dashboard.set_defaults(func=cmd_dashboard)

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
