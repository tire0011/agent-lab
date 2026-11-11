"""#636: generated tests go to the paid coder and the reviewer, marked as model-written.

They are a suggestion: the generated file is taken out of the worktree before the
paid coder runs, the final diff check never requires it, and the benchmark
measures how many of them fail on start and pass on merge.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from modules import local_coding_worker
from modules.benchmark import (
    BenchmarkError,
    BenchmarkRunner,
    BenchmarkTask,
    format_test_first_report,
    load_run,
)
from modules.coding_worker import WorkerResult
from modules.experiment_adoption_service import ExperimentAdoptionService
from modules.experiment_evaluation import (
    evaluate_implementation,
    extract_explicit_goal_artifacts,
    parse_git_status_short,
)
from modules.experiment_handover import (
    MODEL_WRITTEN_TESTS_HEADING,
    model_written_tests_block,
    with_model_written_tests,
)
from modules.experiment_service import ExperimentService
from modules.local_coding_worker import LocalCodingWorker
from modules.nexus_reviewer import NexusReviewer, ReviewResult, review_prompt
from modules.run_store import init_db

GENERATED_REL = "tests/test_generated.py"
GENERATED_ID = "tests.test_generated.GeneratedTests.test_feature"
GENERATED_CONTENT = (
    "import pathlib\nimport unittest\n\n\nclass GeneratedTests(unittest.TestCase):\n"
    "    def test_feature(self):\n"
    "        self.assertTrue(pathlib.Path('feature.txt').exists())\n"
)


def _git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _result(provider: str, task: str, working_dir: str, *, error: str | None = None) -> WorkerResult:
    return WorkerResult(
        success=error is None, exit_code=0 if error is None else 1, stdout="", stderr="",
        runtime_ms=5, timed_out=False, stdout_truncated=False, stderr_truncated=False,
        thread_id=None, final_message="done", usage=None, task=task, working_dir=working_dir,
        sandbox="workspace-write", error=error, provider=provider,
    )


class _LocalWithGeneratedTests:
    """Installs the generated test file like LocalCodingWorker and fails on it."""

    provider = "local"

    def __init__(self, *, keep_tests: bool = True, succeed: bool = False):
        self.keep_tests = keep_tests
        self.succeed = succeed
        self.generated_tests: tuple[str, ...] = ()
        self.generated_test_file: str | None = None
        self.generated_test_content = ""
        self.local_attempt_passed_generated_tests: bool | None = None

    def run(self, *, task, working_dir, **_kwargs):
        if self.keep_tests:
            target = Path(working_dir) / GENERATED_REL
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(GENERATED_CONTENT, encoding="utf-8")
            self.generated_tests = (GENERATED_ID,)
            self.generated_test_file = GENERATED_REL
            self.generated_test_content = GENERATED_CONTENT
        if self.succeed:
            (Path(working_dir) / "feature.txt").write_text("local\n", encoding="utf-8")
            self.local_attempt_passed_generated_tests = True if self.keep_tests else None
            return _result("local", task, working_dir)
        if self.keep_tests:
            self.local_attempt_passed_generated_tests = False
            return _result("local", task, working_dir, error="generated tests still fail")
        # No generated tests: the local context cannot hold the task (#569).
        return _result("local", task, working_dir,
                       error="context_too_small: prompt need 9999 tokens, but n_ctx 4096")


class _Paid:
    provider = "codex"

    def __init__(self):
        self.tasks: list[str] = []
        self.generated_file_present: list[bool] = []

    def run(self, *, task, working_dir, **_kwargs):
        self.tasks.append(task)
        self.generated_file_present.append((Path(working_dir) / GENERATED_REL).exists())
        (Path(working_dir) / "feature.txt").write_text("paid\n", encoding="utf-8")
        return _result("codex", task, working_dir)


class HandoverBlockTests(unittest.TestCase):
    def test_block_is_marked_and_holds_the_file(self):
        block = model_written_tests_block(GENERATED_REL, GENERATED_CONTENT, (GENERATED_ID,))
        self.assertTrue(block.startswith(MODEL_WRITTEN_TESTS_HEADING))
        self.assertEqual("MODEL-WRITTEN TESTS (may be rejected)", MODEL_WRITTEN_TESTS_HEADING)
        self.assertIn(GENERATED_REL, block)
        self.assertIn(GENERATED_ID, block)
        self.assertIn("def test_feature", block)
        self.assertIn("not required in the final diff", block)
        self.assertEqual(f"task\n\n{block}", with_model_written_tests("task", block))

    def test_no_tests_no_block(self):
        self.assertEqual("", model_written_tests_block(None, GENERATED_CONTENT))
        self.assertEqual("", model_written_tests_block(GENERATED_REL, ""))
        self.assertEqual("task", with_model_written_tests("task", ""))

    def test_long_file_is_clipped(self):
        block = model_written_tests_block(GENERATED_REL, "x" * 20000)
        self.assertLess(len(block), 9000)


class LocalWorkerRecordsTheFileTests(unittest.TestCase):
    def test_installed_generated_file_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-q")
            (repo / "mod.py").write_text("VALUE = 1\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "base")

            class Client:
                async def context_size(self):
                    return 8192

                async def count_tokens(self, text):
                    return len(text) // 4

                async def complete(self, *_a, **_k):
                    return "no patch here"

            ids = ["tests.test_generated.ValueTests.test_value"]
            content = ("import unittest\nimport mod\n\nclass ValueTests(unittest.TestCase):\n"
                       "    def test_value(self):\n        self.assertEqual(mod.VALUE, 2)\n")
            with mock.patch.object(local_coding_worker, "generate_failing_tests",
                                   return_value=(ids, content)):
                worker = LocalCodingWorker(client=Client(), max_tokens=512, test_first=True)
                worker.run(task="## Change\n`mod.py`\n## Test\nVALUE is 2",
                           working_dir=str(repo), sandbox="workspace-write")
            self.assertEqual("tests/test_generated.py", worker.generated_test_file)
            self.assertEqual(content, worker.generated_test_content)
            self.assertEqual(tuple(ids), worker.generated_tests)


class PaidFallbackHandoverTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        (self.repo / "value.txt").write_text("original\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        self.paid = _Paid()
        self.service = ExperimentService(
            db_path=str(root / "agent.db"), repo_root=str(self.repo),
            worktrees_root=str(root / "worktrees"),
            worker_factory=lambda _p: self.paid,
            test_command=["/bin/true"], max_repair_iterations=0,
            experiment_log_dir=str(root / "logs"),
        )
        self.addCleanup(self.service.close)

    def _run(self, local):
        self.service.local_worker_factory = lambda _p: local
        exp = self.service.create_experiment(
            "Add feature.txt", complexity="small", source_type="issue", source_id="636"
        )
        return self.service.run_experiment(exp.id)

    def test_handover_contains_the_marked_block_when_tests_exist(self):
        result = self._run(_LocalWithGeneratedTests())

        self.assertEqual("codex", result.implementation_provider)
        self.assertEqual(1, len(self.paid.tasks))
        self.assertIn(MODEL_WRITTEN_TESTS_HEADING, self.paid.tasks[0])
        self.assertIn("def test_feature", self.paid.tasks[0])
        # Not forced into the diff: the paid coder never finds the file in the tree.
        self.assertEqual([False], self.paid.generated_file_present)
        self.assertNotIn(GENERATED_REL, result.git_status or "")
        self.assertIn(MODEL_WRITTEN_TESTS_HEADING, result.model_written_tests or "")
        # The goal itself is unchanged, so no later check reads the block as task text.
        self.assertNotIn(MODEL_WRITTEN_TESTS_HEADING, result.goal)

    def test_a_repair_coder_gets_the_block_too(self):
        self.service.max_repair_iterations = 1
        self.service.test_command = ["/bin/false"]
        self._run(_LocalWithGeneratedTests())

        self.assertEqual(2, len(self.paid.tasks))
        self.assertIn("did not pass verification", self.paid.tasks[1])
        self.assertIn(MODEL_WRITTEN_TESTS_HEADING, self.paid.tasks[1])

    def test_no_block_when_no_tests_were_generated(self):
        result = self._run(_LocalWithGeneratedTests(keep_tests=False))

        self.assertEqual("codex", result.implementation_provider)
        self.assertEqual(1, len(self.paid.tasks))
        self.assertNotIn(MODEL_WRITTEN_TESTS_HEADING, self.paid.tasks[0])
        self.assertIsNone(result.model_written_tests)


class FinalDiffCheckTests(unittest.TestCase):
    def test_goal_artifacts_ignore_the_marked_block(self):
        block = model_written_tests_block("modules/generated_helper.py", "x = 1\n")
        goal = with_model_written_tests("## Change\nEdit `modules/real.py`.", block)
        self.assertEqual(["modules/real.py"], extract_explicit_goal_artifacts(goal))

    def test_a_diff_without_the_generated_file_passes(self):
        block = model_written_tests_block(GENERATED_REL, GENERATED_CONTENT, (GENERATED_ID,))
        goal = with_model_written_tests("## Change\nEdit `modules/real.py`.", block)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "modules").mkdir()
            (Path(tmp) / "modules" / "real.py").write_text("x = 1\n")
            result = evaluate_implementation(
                None, goal, parse_git_status_short(" M modules/real.py\n"), tmp
            )
        self.assertEqual("pass", result.outcome, result.reason)
        self.assertNotIn(GENERATED_REL, result.missing_artifacts)


class ReviewerTests(unittest.TestCase):
    BLOCK = model_written_tests_block(GENERATED_REL, GENERATED_CONTENT, (GENERATED_ID,))

    def test_prompt_contains_the_block_only_when_given(self):
        with_block = review_prompt("task", "diff", can_read_files=True, model_written_tests=self.BLOCK)
        self.assertIn(MODEL_WRITTEN_TESTS_HEADING, with_block)
        self.assertIn("not a reason to block", with_block)
        self.assertLess(with_block.index(MODEL_WRITTEN_TESTS_HEADING), with_block.index("DIFF:"))
        self.assertNotIn(MODEL_WRITTEN_TESTS_HEADING, review_prompt("task", "diff", can_read_files=True))

    def test_reviewer_passes_the_block_to_the_model(self):
        prompts: list[str] = []

        def runner(prompt, _path):
            prompts.append(prompt)
            return '{"verdict": "approve", "summary": "fine", "problems": []}'

        with tempfile.TemporaryDirectory() as tmp:
            reviewer = NexusReviewer(db_path=str(Path(tmp) / "r.db"),
                                     runners={"claude": runner, "codex": runner, "azure": runner})
            with mock.patch("modules.nexus_reviewer._diff", return_value="diff"):
                reviewer.review(experiment_id="e", goal="g", worktree_path=tmp, base_sha="b",
                                model_written_tests=self.BLOCK)
                reviewer.review(experiment_id="f", goal="g", worktree_path=tmp, base_sha="b")
        self.assertIn(MODEL_WRITTEN_TESTS_HEADING, prompts[0])
        self.assertNotIn(MODEL_WRITTEN_TESTS_HEADING, prompts[1])

    def test_adoption_hands_the_stored_block_to_the_reviewer(self):
        reviewer = mock.MagicMock()
        reviewer.review.return_value = ReviewResult("claude", "approve", "ok")
        service = ExperimentAdoptionService.__new__(ExperimentAdoptionService)
        service.reviewer = reviewer
        service.github_work_tracking = mock.MagicMock()
        service._integration_base_sha = lambda: "base"

        service._review_blocks("e", "g", SimpleNamespace(pr_number=1), "/tmp/x", None,
                               model_written_tests=self.BLOCK)
        self.assertEqual(self.BLOCK, reviewer.review.call_args.kwargs["model_written_tests"])
        service._review_blocks("e", "g", SimpleNamespace(pr_number=1), "/tmp/x", None)
        self.assertNotIn("model_written_tests", reviewer.review.call_args.kwargs)


class BenchmarkTestFirstTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = str(self.root / "repo")
        Path(self.repo).mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        (Path(self.repo) / "tests").mkdir()
        (Path(self.repo) / "tests" / "conftest.py").write_text("", encoding="utf-8")
        (Path(self.repo) / "tests" / "test_ok.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
            encoding="utf-8",
        )
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.base = _git(self.repo, "rev-parse", "HEAD")
        self.db = str(self.root / "benchmark.db")

    def task(self):
        return BenchmarkTask(
            id="t1", goal="GitHub Issue #9636: Add feature.txt to this repository.",
            base_commit=self.base,
            expected_files=("feature.txt",), test_modules=("tests.test_ok",),
            tests_green_points=50, expected_files_points=30, evaluation_points=20,
            sha256="a" * 64,
        )

    def run_bench(self, local, paid, test_first):
        runner = BenchmarkRunner(
            repo_root=self.repo, db_path=self.db, worktrees_root=str(self.root / "wt"),
            test_python_executable=sys.executable, provider="codex",
            worker_factory=lambda _p: paid, local_worker_factory=lambda _p: local,
            price_db_path=str(self.root / "no-such.db"), read_rss=lambda: 1,
            experiment_log_dir=str(self.root / "logs"), test_first=test_first,
        )
        run_id = runner.run([self.task()])
        conn = init_db(self.db)
        try:
            return run_id, load_run(conn, run_id)["t1"]
        finally:
            conn.close()

    def test_test_first_on_measures_generated_tests_and_paid_calls(self):
        paid = _Paid()
        run_on, row = self.run_bench(_LocalWithGeneratedTests(), paid, True)

        self.assertEqual("succeeded", row["status"], row["error"])
        self.assertEqual(1, row["test_first"])
        self.assertEqual(1, row["tests_generated"])
        self.assertEqual(1, row["generated_tests_failing_on_start"])
        self.assertEqual(1, row["generated_tests_passing_on_merge"])
        self.assertEqual(0, row["local_only_success"])
        self.assertEqual(1, row["paid_calls"])
        self.assertIn(MODEL_WRITTEN_TESTS_HEADING, paid.tasks[0])
        # The probe file used for the measurement does not stay behind.
        self.assertEqual([], list((self.root / "wt").glob("**/_nexus_bench_generated_*")))

        run_off, off_row = self.run_bench(
            _LocalWithGeneratedTests(keep_tests=False, succeed=True), _Paid(), False
        )
        self.assertEqual((0, 0, 1, 0), (off_row["test_first"], off_row["tests_generated"],
                                        off_row["local_only_success"], off_row["paid_calls"]))
        self.assertIsNone(off_row["generated_tests_passing_on_merge"])

        conn = init_db(self.db)
        try:
            report = format_test_first_report(conn, run_on, run_off)
            with self.assertRaises(BenchmarkError):
                format_test_first_report(conn, run_off, run_on)
        finally:
            conn.close()
        self.assertIn("| Generated tests that fail on start | 0 | 1 |", report)
        self.assertIn("| ... of them passing on merge | n/a | 1 |", report)
        self.assertIn("| Local-only success rate | 100% (1/1) | 0% (0/1) |", report)
        self.assertIn("| Paid calls per task | 0.00 | 1.00 |", report)

    def test_an_earlier_local_failure_does_not_route_a_later_run_to_paid(self):
        # The same issue failed locally in the first run (in the same database),
        # with no paid fallback, like a local patch that never applies.
        class _LocalFails(_LocalWithGeneratedTests):
            def run(self, *, task, working_dir, **_kwargs):
                return _result("local", task, working_dir, error="local model patch failed")

        _first, first = self.run_bench(_LocalFails(keep_tests=False), _Paid(), True)
        self.assertEqual(("failed", "local"), (first["status"], first["provider"]))
        local = _LocalWithGeneratedTests(keep_tests=False, succeed=True)
        _run_id, row = self.run_bench(local, _Paid(), False)
        self.assertEqual("local", row["provider"])
        self.assertEqual((1, 0), (row["local_only_success"], row["paid_calls"]))

    def test_without_test_first_the_benchmark_stays_as_before(self):
        paid = _Paid()
        local = mock.Mock(side_effect=AssertionError("local model must not run"))
        _run_id, row = self.run_bench(local, paid, None)
        self.assertEqual("codex", row["provider"])
        self.assertIsNone(row["test_first"])
        self.assertIsNone(row["local_only_success"])
        self.assertEqual(1, row["paid_calls"])

    def test_an_old_benchmark_table_gains_the_columns(self):
        db = str(self.root / "old.db")
        conn = sqlite3.connect(db)
        # The table as #343 created it, before the test-first columns.
        conn.execute(
            "CREATE TABLE benchmark_runs (run_id TEXT NOT NULL, task_id TEXT NOT NULL, "
            "status TEXT NOT NULL, score REAL NOT NULL, started_at TEXT NOT NULL, "
            "PRIMARY KEY (run_id, task_id))"
        )
        conn.execute("INSERT INTO benchmark_runs VALUES ('old', 't', 'succeeded', 1, 't')")
        conn.commit()
        conn.close()
        conn = init_db(db)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(benchmark_runs)")}
            kept = conn.execute("SELECT test_first, paid_calls FROM benchmark_runs").fetchall()
        finally:
            conn.close()
        self.assertTrue({"test_first", "generated_tests_passing_on_merge", "paid_calls",
                         "local_only_success"} <= columns)
        self.assertEqual([(None, None)], kept)


if __name__ == "__main__":
    unittest.main()
