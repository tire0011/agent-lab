"""#817: whitespace-only ``git diff --check`` findings are fixed in the worktree
for free instead of ending a verified run as ``needs_review``."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.experiment_evaluation import EvaluationResult
from modules.experiment_service import ExperimentService, STATUS_VERIFYING
from modules.experiment_service.whitespace_fix import (
    _fix_file,
    _safe_target,
    fix_whitespace,
    parse_diff_check_report,
)


class WhitespaceFixUnitTests(unittest.TestCase):
    """`fix_whitespace` on a temp git repo; no network involved."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=check, text=True, capture_output=True,
        )

    def _write(self, rel: str, content: str | bytes) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))

    def _read(self, rel: str) -> str:
        return (self.repo / rel).read_text(encoding="utf-8")

    def _commit_all(self, message: str = "base") -> None:
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)

    def _fix(self):
        return fix_whitespace(str(self.repo), "HEAD")

    def _plain_check_report(self) -> str:
        proc = subprocess.run(
            ["git", "diff", "--check", "HEAD"], cwd=self.repo, capture_output=True, text=True,
        )
        return proc.stdout.strip()

    def test_trailing_whitespace_on_changed_line_fixed_unreported_line_untouched(self) -> None:
        # Line 1 already has trailing whitespace in the base and is NOT changed,
        # so git does not report it; it must survive the fix untouched.
        self._write("code.py", "alpha = 1  \nbeta = 2\n")
        self._commit_all()
        self._write("code.py", "alpha = 1  \nbeta = 2\ngamma = 3   \n")

        result = self._fix()

        self.assertEqual(result.remaining_report, "")
        self.assertEqual(result.fixed_files, {"code.py": 1})
        self.assertEqual(self._read("code.py"), "alpha = 1  \nbeta = 2\ngamma = 3\n")
        self.assertEqual(self._plain_check_report(), "")

    def test_new_blank_line_at_eof_is_removed(self) -> None:
        self._write("notes.txt", "hello\n")
        self._commit_all()
        self._write("notes.txt", "hello\n\n\n")

        result = self._fix()

        self.assertEqual(result.remaining_report, "")
        self.assertEqual(result.fixed_files, {"notes.txt": 2})
        self.assertEqual(self._read("notes.txt"), "hello\n")

    def test_conflict_marker_stays_in_remaining_report(self) -> None:
        self._write("f.py", "base\n")
        self._commit_all()
        self._write("f.py", "base\n<<<<<<< HEAD\nours\n")

        result = self._fix()

        self.assertEqual(result.fixed_files, {})
        self.assertIn("leftover conflict marker", result.remaining_report)
        self.assertIn("f.py", result.remaining_report)
        self.assertEqual(self._read("f.py"), "base\n<<<<<<< HEAD\nours\n")

    def test_markdown_line_ending_in_two_spaces_is_not_changed(self) -> None:
        self._write("doc.md", "# Title\n\nold\n")
        self._commit_all()
        # A changed line ending in exactly two spaces is a Markdown hard break.
        self._write("doc.md", "# Title\n\nnew  \n")

        result = self._fix()

        self.assertNotIn("doc.md", result.fixed_files)
        self.assertEqual(self._read("doc.md"), "# Title\n\nnew  \n")

    def test_markdown_line_ending_in_three_spaces_is_stripped(self) -> None:
        self._write("doc.md", "# Title\n\nold\n")
        self._commit_all()
        self._write("doc.md", "# Title\n\nnew   \n")

        result = self._fix()

        self.assertEqual(result.remaining_report, "")
        self.assertEqual(result.fixed_files, {"doc.md": 1})
        self.assertEqual(self._read("doc.md"), "# Title\n\nnew\n")

    def test_space_before_tab_in_indent_is_fixed(self) -> None:
        self._write("s.py", "x = 1\n")
        self._commit_all()
        self._write("s.py", "x = 1\n \ty = 2\n")

        result = self._fix()

        self.assertEqual(result.remaining_report, "")
        self.assertEqual(self._read("s.py"), "x = 1\n\ty = 2\n")

    def test_new_untracked_file_with_trailing_whitespace_is_fixed(self) -> None:
        self._write("base.py", "x = 1\n")
        self._commit_all()
        self._write("new_file.py", "a = 1  \n")  # untracked, only visible via a full index

        result = self._fix()

        self.assertEqual(result.remaining_report, "")
        self.assertEqual(result.fixed_files, {"new_file.py": 1})
        self.assertEqual(self._read("new_file.py"), "a = 1\n")

    def test_binary_file_is_never_touched(self) -> None:
        target = self.repo / "blob.bin"
        payload = b"\x00\x01\x02  \n"
        target.write_bytes(payload)

        changed = _fix_file(target, "blob.bin", [(1, "trailing whitespace.")])

        self.assertEqual(changed, 0)
        self.assertEqual(target.read_bytes(), payload)

    def test_paths_outside_the_worktree_are_rejected(self) -> None:
        root = self.repo.resolve()
        self.assertIsNone(_safe_target(Path(self.repo), "../outside.py"))
        self.assertIsNone(_safe_target(Path(self.repo), "/etc/passwd"))
        self.assertEqual(_safe_target(Path(self.repo), "sub/ok.py"), (root / "sub" / "ok.py"))

    def test_report_parsing_skips_echoed_diff_lines(self) -> None:
        report = (
            "a.py:2: trailing whitespace.\n"
            "+x.py:9: trailing whitespace.\n"  # echoed diff line, not a finding
            "b.py:1: leftover conflict marker\n"
        )
        findings = parse_diff_check_report(report)
        self.assertEqual(
            findings,
            {
                "a.py": [(2, "trailing whitespace.")],
                "b.py": [(1, "leftover conflict marker")],
            },
        )


class WhitespaceAutofixVerificationTests(unittest.TestCase):
    """Integration through the verify path with a fake test command."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        self.repo = root / "repo"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "app.py").write_text("VALUE = 'good'\n", encoding="utf-8")
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "base")
        self.service = ExperimentService(
            db_path=str(root / "db.sqlite"), repo_root=str(self.repo),
            worktrees_root=str(root / "worktrees"), test_python_executable=sys.executable,
            experiment_log_dir=str(root / "logs"), max_repair_iterations=0,
            worker_factory=lambda _: None, plan_worker_factory=lambda _: None,
        )
        self.addCleanup(self.service.close)
        # A fake test command that always passes.
        self.service.test_command = [sys.executable, "-c", "import sys; sys.exit(0)"]
        # Keep the deterministic quality gate out of the way: these tests are
        # about the diff-check autofix.
        quality = patch(
            "modules.experiment_service.evaluate_implementation",
            return_value=EvaluationResult(outcome="pass", reason="ok"),
        )
        quality.start()
        self.addCleanup(quality.stop)

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, text=True, capture_output=True,
        ).stdout

    def _verify(self):
        experiment = self.service.create_experiment("Change app behavior")
        base = self._git("rev-parse", "HEAD").strip()
        self.service._force_status(experiment.id, STATUS_VERIFYING, base_commit_sha=base)
        with patch.dict("os.environ", {"NEXUS_TIMING_CHECK": "0"}):
            result = self.service._verify(experiment.id, str(self.repo))
        return experiment.id, result

    def _log_events(self, experiment_id: str) -> list[dict]:
        path = self.root / "logs" / f"{experiment_id}.jsonl"
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
        return events

    def test_whitespace_only_diff_check_ends_succeeded_and_logs_autofix(self) -> None:
        (self.repo / "app.py").write_text("VALUE = 'good'\nNEW = 1   \n", encoding="utf-8")

        experiment_id, result = self._verify()

        self.assertEqual(result.status, "succeeded", result.error)
        self.assertIsNone(result.error)
        # The worktree file was actually fixed.
        self.assertEqual((self.repo / "app.py").read_text(encoding="utf-8"), "VALUE = 'good'\nNEW = 1\n")
        # diff_check_autofixed is logged with the files and the line count.
        autofix = [e for e in self._log_events(experiment_id) if e.get("event") == "diff_check_autofixed"]
        self.assertEqual(len(autofix), 1)
        self.assertIn("app.py", autofix[0].get("files", []))
        self.assertGreaterEqual(autofix[0].get("lines_fixed", 0), 1)
        # The auto-fix is mentioned in the verification summary (stored test output).
        self.assertIn("diff_check_autofixed", result.test_output or "")

    def test_conflict_marker_still_ends_needs_review(self) -> None:
        (self.repo / "app.py").write_text("VALUE = 'good'\n<<<<<<< HEAD\n", encoding="utf-8")

        experiment_id, result = self._verify()

        self.assertEqual(result.status, "needs_review")
        self.assertTrue(
            (result.error or "").startswith("git diff --check reported issues"),
            result.error,
        )
        self.assertIn("leftover conflict marker", result.error or "")
        self.assertEqual((self.repo / "app.py").read_text(encoding="utf-8"), "VALUE = 'good'\n<<<<<<< HEAD\n")
        autofix = [e for e in self._log_events(experiment_id) if e.get("event") == "diff_check_autofixed"]
        self.assertEqual(autofix, [])


if __name__ == "__main__":
    unittest.main()
