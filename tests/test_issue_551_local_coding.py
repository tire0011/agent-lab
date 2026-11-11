from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from modules.coding_worker import WorkerResult
from modules.experiment_service import ExperimentService
from modules.local_coding_worker import LocalCodingWorker


class _Client:
    def __init__(self, response: str):
        self.response = response
        self.prompt = ""

    async def complete(self, prompt: str, **_kwargs):
        self.prompt = prompt
        return self.response


class _Worker:
    def __init__(self, provider: str, calls: list[str]):
        self.provider = provider
        self.calls = calls

    def run(self, *, working_dir: str, task: str, sandbox: str, **_kwargs):
        self.calls.append(self.provider)
        Path(working_dir, "value.txt").write_text("changed\n")
        return WorkerResult(True, 0, "", "", 1, False, False, False, None, None,
                            None, task, working_dir, sandbox, None, self.provider)


class Issue551LocalCodingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        (self.repo / "value.txt").write_text("original\n")
        subprocess.run(["git", "add", "value.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        self.db = str(root / "agent.db")
        self.calls: list[str] = []
        self.service = ExperimentService(
            db_path=self.db, repo_root=str(self.repo), worktrees_root=str(root / "worktrees"),
            local_worker_factory=lambda _p: _Worker("local", self.calls),
            worker_factory=lambda _p: _Worker("codex", self.calls),
            test_command=["/bin/true"], max_repair_iterations=0,
            experiment_log_dir=str(root / "logs"),
        )

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def test_small_uses_local_and_large_or_missing_use_external(self):
        for complexity, expected, source_id in (
            ("small", "local", "small"), ("large", "codex", "large"),
            (None, "codex", "missing"), ("unknown", "codex", "unknown"),
        ):
            exp = self.service.create_experiment(
                f"change value {source_id}", complexity=complexity,
                source_type="task", source_id=source_id,
            )
            result = self.service.run_experiment(exp.id)
            self.assertEqual(expected, result.implementation_provider)
            self.assertEqual("small" if complexity == "small" else "large", result.complexity)
            self.assertEqual("needs_review", result.status)  # all providers reached shared verification

    def test_non_successful_local_statuses_force_external_for_same_issue(self):
        for status in ("failed", "timed_out", "needs_review", "interrupted"):
            source_id = f"551-{status}"
            first = self.service.create_experiment("first", complexity="small", source_type="issue", source_id=source_id)
            with self.service._conn:
                self.service._conn.execute(
                    "UPDATE experiments SET implementation_provider='local', status=? WHERE id=?",
                    (status, first.id),
                )
            later = self.service.create_experiment("changed handover", complexity="small", source_type="issue", source_id=source_id)
            self.assertTrue(self.service._must_use_external_provider(later))

    def test_mailbox_identity_not_goal_and_success_does_not_force_fallback(self):
        failed = self.service.create_experiment("old goal", complexity="small", source_type="task", source_id="mailbox-1")
        succeeded = self.service.create_experiment("other", complexity="small", source_type="task", source_id="mailbox-2")
        with self.service._conn:
            self.service._conn.execute("UPDATE experiments SET implementation_provider='local', status='failed' WHERE id=?", (failed.id,))
            self.service._conn.execute("UPDATE experiments SET implementation_provider='local', status='succeeded' WHERE id=?", (succeeded.id,))
        retry = self.service.create_experiment("entirely different goal and handover", complexity="small", source_type="task", source_id="mailbox-1")
        okay = self.service.create_experiment("new goal", complexity="small", source_type="task", source_id="mailbox-2")
        self.assertTrue(self.service._must_use_external_provider(retry))
        self.assertFalse(self.service._must_use_external_provider(okay))

    def test_local_worker_applies_patch_and_rejects_non_applying_patch(self):
        good = _Client("--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-original\n+patched\n")
        result = LocalCodingWorker(client=good).run(
            task="Edit value.txt", working_dir=str(self.repo), sandbox="workspace-write"
        )
        self.assertTrue(result.success)
        self.assertEqual("patched\n", (self.repo / "value.txt").read_text())
        self.assertIn("original", good.prompt)  # named file contents were supplied

        bad = _Client("--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-not-there\n+x\n")
        result = LocalCodingWorker(client=bad).run(
            task="Edit value.txt", working_dir=str(self.repo), sandbox="workspace-write"
        )
        self.assertFalse(result.success)
        self.assertIn("patch does not apply", result.error or "")

    def test_non_applying_local_patch_fails_the_experiment_with_reason(self):
        client = _Client("--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-not-there\n+x\n")
        self.service.local_worker_factory = lambda _p: LocalCodingWorker(client=client)
        exp = self.service.create_experiment(
            "Edit value.txt", complexity="small", source_type="issue", source_id="551-bad-patch"
        )
        result = self.service.run_experiment(exp.id)
        self.assertEqual("failed", result.status)
        self.assertEqual("local", result.implementation_provider)
        self.assertIn("patch does not apply", result.error or "")

    def test_failed_generated_acceptance_tests_trigger_paid_fallback(self):
        class GeneratedFailure(_Worker):
            generated_tests = ("tests.test_generated.T.test_value",)
            local_attempt_passed_generated_tests = False

            def run(self, **kwargs):
                self.calls.append(self.provider)
                return WorkerResult(False, 1, "", "generated tests still fail", 1,
                                    False, False, False, None, None, None,
                                    kwargs["task"], kwargs["working_dir"], kwargs["sandbox"],
                                    None, self.provider)

        self.service.local_worker_factory = lambda _p: GeneratedFailure("local", self.calls)
        exp = self.service.create_experiment(
            "change value", complexity="small", source_type="issue", source_id="635-fallback"
        )
        result = self.service.run_experiment(exp.id)
        self.assertEqual(["local", "codex"], self.calls)
        self.assertEqual("codex", result.implementation_provider)

    def test_failed_local_attempt_without_generated_test_failure_uses_no_fallback(self):
        class UnrelatedFailure(_Worker):
            generated_tests = ("tests.test_generated.T.test_value",)
            local_attempt_passed_generated_tests = True

            def run(self, **kwargs):
                self.calls.append(self.provider)
                return WorkerResult(False, 1, "", "later unrelated subtask failed", 1,
                                    False, False, False, None, None, None,
                                    kwargs["task"], kwargs["working_dir"], kwargs["sandbox"],
                                    None, self.provider)

        self.service.local_worker_factory = lambda _p: UnrelatedFailure("local", self.calls)
        exp = self.service.create_experiment(
            "change value", complexity="small", source_type="issue", source_id="635-no-fallback"
        )
        result = self.service.run_experiment(exp.id)
        self.assertEqual(["local"], self.calls)
        self.assertEqual("local", result.implementation_provider)


if __name__ == "__main__":
    unittest.main()
