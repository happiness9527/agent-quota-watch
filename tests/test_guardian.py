import contextlib
import io
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import guardian


class GuardianParsingTests(unittest.TestCase):
    def test_parse_compact_duration(self):
        self.assertEqual(guardian.parse_duration("5h10m"), timedelta(hours=5, minutes=10))
        self.assertEqual(guardian.parse_duration("30 minutes"), timedelta(minutes=30))
        self.assertEqual(guardian.parse_duration("1 hour 15 min"), timedelta(hours=1, minutes=15))

    def test_detects_quota_text(self):
        self.assertTrue(guardian.is_quota_text("Usage limit reached. Try again in 5 hours."))
        self.assertTrue(guardian.is_quota_text("You've reached your 5-hour limit. It resets at 12:20 PM."))
        self.assertTrue(guardian.is_quota_text("You’ve hit your limit for Claude messages."))
        self.assertTrue(guardian.is_quota_text("HTTP 429 rate limited"))
        self.assertTrue(guardian.is_quota_text("Quota exceeded. Please retry later."))
        self.assertFalse(guardian.is_quota_text("Unit tests failed with exit code 1"))
        self.assertFalse(guardian.is_quota_text("We are discussing quota automation design"))

    def test_infer_retry_at_from_duration(self):
        now = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
        retry_at = guardian.infer_retry_at("try again in 2 hours 30 minutes", now=now)
        self.assertEqual(retry_at, now + timedelta(hours=2, minutes=30))

    def test_default_retry_when_duration_missing(self):
        now = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
        retry_at = guardian.infer_retry_at("quota exceeded", now=now)
        self.assertEqual(retry_at, now + guardian.DEFAULT_RETRY_DELAY)

    def test_infer_retry_at_from_reset_wall_time(self):
        now = datetime(2026, 6, 6, 23, 9, tzinfo=timezone.utc)
        retry_at = guardian.infer_retry_at("Usage limit reached • Resets 2:30 AM", now=now)
        self.assertEqual(
            retry_at,
            datetime(2026, 6, 7, 2, 32, tzinfo=timezone.utc),
        )

    def test_infer_retry_at_from_midday_reset_wall_time(self):
        now = datetime(2026, 6, 7, 9, 10, tzinfo=timezone.utc)
        retry_at = guardian.infer_retry_at("Usage limit reached • Resets 12:20 PM", now=now)
        self.assertEqual(
            retry_at,
            datetime(2026, 6, 7, 12, 22, tzinfo=timezone.utc),
        )

    def test_infer_retry_at_from_chinese_reset_wall_time(self):
        now = datetime(2026, 6, 6, 23, 9, tzinfo=timezone.utc)
        retry_at = guardian.infer_retry_at("额度已用尽，凌晨2:30恢复", now=now)
        self.assertEqual(
            retry_at,
            datetime(2026, 6, 7, 2, 32, tzinfo=timezone.utc),
        )


class GuardianCommandTests(unittest.TestCase):
    def make_task(self, platform="claude", session="last"):
        with tempfile.TemporaryDirectory() as tmp:
            task = guardian.make_task(
                platform=platform,
                cwd=tmp,
                prompt="finish the task",
                name=None,
                session=session,
                auto_resume=True,
            )
            return task

    def test_claude_resume_last_command(self):
        task = self.make_task("claude", "last")
        command = guardian.build_command(task, resume=True)
        self.assertEqual(command[:3], ["claude", "-p", "--continue"])
        self.assertIn("Checkpoint 文件", command[-1])

    def test_claude_resume_session_command(self):
        task = self.make_task("claude", "abc123")
        command = guardian.build_command(task, resume=True)
        self.assertEqual(command[:4], ["claude", "-p", "--resume", "abc123"])

    def test_codex_resume_last_command(self):
        task = self.make_task("codex", "last")
        command = guardian.build_command(task, resume=True)
        self.assertEqual(command[:4], ["codex", "exec", "resume", "--last"])
        self.assertIn("Checkpoint 文件", command[-1])

    def test_codex_start_command(self):
        task = self.make_task("codex", "last")
        command = guardian.build_command(task, resume=False)
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertIn("用户任务", command[-1])

    def test_claude_app_resume_command(self):
        task = self.make_task("claude-app", "ui")
        command = guardian.build_command(task, resume=True)
        self.assertEqual(command[0], "osascript")
        self.assertNotIn('clickButtonByName(front window, "Keep working")', command[-1])
        self.assertIn("Wait until", command[-1])
        self.assertIn("submitContinuationPrompt", command[-1])
        self.assertIn("请继续完成刚才因为额度限制中断的任务", command[-1])
        self.assertIn("findQuotaText", command[-1])

    def test_claude_app_resume_action_description(self):
        task = self.make_task("claude-app", "ui")
        description = guardian.resume_action_description(task)
        self.assertIn("自动发送继续任务提示", description)
        self.assertNotIn("Keep working", description)

    def test_register_claude_app_limit_text_dry_run(self):
        task = guardian.register_claude_app_limit_text(
            "Usage limit reached • Resets 2:30 AM • Keep working",
            cwd=tempfile.gettempdir(),
            auto_resume=True,
            dry_run=True,
        )
        self.assertEqual(task["platform"], "claude-app")
        self.assertEqual(task["status"], "rate_limited")
        self.assertTrue(task["retry_at"])

    def test_register_scheduled_codex_task_dry_run(self):
        task = guardian.register_discovered_task(
            platform="codex",
            cwd=tempfile.gettempdir(),
            session="session-1",
            prompt="near quota",
            retry_at=datetime(2026, 6, 7, 2, 17, tzinfo=timezone.utc),
            source_key="codex-rollout:session-1",
            source="/tmp/rollout.jsonl",
            auto_resume=True,
            dry_run=True,
            status="scheduled",
        )
        self.assertEqual(task["platform"], "codex")
        self.assertEqual(task["status"], "scheduled")


class GuardianTaskStateTests(unittest.TestCase):
    def test_due_for_resume_requires_auto_resume(self):
        task = {
            "status": "rate_limited",
            "auto_resume": False,
            "retry_at": guardian.iso(guardian.local_now() - timedelta(minutes=1)),
        }
        self.assertFalse(guardian.due_for_resume(task))

    def test_due_for_resume_when_retry_time_passed(self):
        task = {
            "status": "rate_limited",
            "auto_resume": True,
            "retry_at": guardian.iso(guardian.local_now() - timedelta(minutes=1)),
        }
        self.assertTrue(guardian.due_for_resume(task))

    def test_completed_task_is_not_manually_resumable(self):
        self.assertFalse(guardian.can_resume_manually({"status": "completed"}))
        self.assertTrue(
            guardian.can_resume_manually(
                {
                    "status": "rate_limited",
                    "retry_at": guardian.iso(guardian.local_now() - timedelta(minutes=1)),
                }
            )
        )
        self.assertFalse(
            guardian.can_resume_manually(
                {
                    "status": "scheduled",
                    "retry_at": guardian.iso(guardian.local_now() + timedelta(hours=1)),
                }
            )
        )


class GuardianCliTests(unittest.TestCase):
    def test_shorthand_allows_options_before_prompt_separator(self):
        argv = guardian.normalize_shorthand(
            ["claude", "--auto-resume", "yes", "--no-wait", "--", "dry", "task"]
        )
        args = guardian.build_parser().parse_args(argv)
        self.assertEqual(args.command, "run")
        self.assertEqual(args.platform, "claude")
        self.assertEqual(args.auto_resume, "yes")
        self.assertTrue(args.no_wait)
        self.assertEqual(args.prompt, ["dry", "task"])

    def test_discover_command_parses(self):
        args = guardian.build_parser().parse_args(["discover", "--dry-run", "--scan-ui"])
        self.assertEqual(args.command, "discover")
        self.assertTrue(args.dry_run)
        self.assertTrue(args.scan_ui)

    def test_update_and_delete_commands_parse(self):
        update_args = guardian.build_parser().parse_args(
            ["update", "task-1", "--retry-after", "5h10m", "--auto-resume", "yes"]
        )
        self.assertEqual(update_args.command, "update")
        self.assertEqual(update_args.retry_after, "5h10m")

        delete_args = guardian.build_parser().parse_args(["delete", "task-1"])
        self.assertEqual(delete_args.command, "delete")

    def test_dashboard_command_parses(self):
        args = guardian.build_parser().parse_args(
            ["dashboard", "--scan-ui", "--open", "--keep-awake", "--interval", "15"]
        )
        self.assertEqual(args.command, "dashboard")
        self.assertTrue(args.scan_ui)
        self.assertTrue(args.open)
        self.assertTrue(args.keep_awake)
        self.assertEqual(args.interval, 15)

    def test_dashboard_port_in_use_returns_success(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), type("Handler", (object,), {}))
        port = server.server_address[1]

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            args = guardian.build_parser().parse_args(
                ["dashboard", "--host", "127.0.0.1", "--port", str(port), "--no-worker"]
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(guardian.cmd_dashboard(args), 0)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
