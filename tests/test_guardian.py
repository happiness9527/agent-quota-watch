import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
        self.assertTrue(guardian.is_quota_text("HTTP 429 rate limited"))
        self.assertFalse(guardian.is_quota_text("Unit tests failed with exit code 1"))

    def test_infer_retry_at_from_duration(self):
        now = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
        retry_at = guardian.infer_retry_at("try again in 2 hours 30 minutes", now=now)
        self.assertEqual(retry_at, now + timedelta(hours=2, minutes=30))

    def test_default_retry_when_duration_missing(self):
        now = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
        retry_at = guardian.infer_retry_at("quota exceeded", now=now)
        self.assertEqual(retry_at, now + guardian.DEFAULT_RETRY_DELAY)


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


if __name__ == "__main__":
    unittest.main()
