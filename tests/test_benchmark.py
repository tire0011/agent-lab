"""#343: fixed benchmark task set, runner and baseline report."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from modules.benchmark import (
    BENCHMARK_TASKS_DIR,
    BenchmarkError,
    BenchmarkRunner,
    BenchmarkTask,
    VERDICT_BETTER,
    VERDICT_EQUAL,
    VERDICT_NOT_COMPARABLE,
    VERDICT_WORSE,
    compare_runs,
    default_benchmark_db_path,
    format_report,
    get_baseline,
    load_run,
    load_task,
    load_tasks,
    process_tree_rss_bytes,
    set_baseline,
)
from modules.codex_worker import CodexWorker
from modules.coding_worker import WorkerResult
from modules.experiment_service.policy import _check_goal_policy
from modules.run_store import init_db

GOAL = "Add feature.txt to this repository."


def _git(cwd: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


class FakeWorker:
    """Writes ``outputs[n]`` to feature.txt on call n; None changes nothing."""

    provider = "claude"

    def __init__(self, outputs, usage=None, error=None):
        self.outputs = list(outputs)
        self.usage = usage
        self.error = error
        self.prompts: list[str] = []
        self.saw_marker: list[bool] = []

    def run(self, *, task, working_dir, **_kwargs):
        self.prompts.append(task)
        self.saw_marker.append((Path(working_dir) / "later.txt").exists())
        output = self.outputs.pop(0)
        if output is not None:
            (Path(working_dir) / "feature.txt").write_text(output, encoding="utf-8")
        return WorkerResult(
            success=self.error is None, exit_code=0, stdout="", stderr="", runtime_ms=5,
            timed_out=False, stdout_truncated=False, stderr_truncated=False,
            thread_id=None, final_message="done", usage=self.usage, task=task,
            working_dir=working_dir, sandbox="workspace-write", error=self.error,
            provider="claude",
        )


class RunnerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = str(self.root / "repo")
        os.mkdir(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        (Path(self.repo) / "tests").mkdir()
        # tests.conftest is what the benchmark's test launcher imports first.
        (Path(self.repo) / "tests" / "conftest.py").write_text("", encoding="utf-8")
        # "broken" lets a fake worker fail verification on purpose.
        (Path(self.repo) / "tests" / "test_ok.py").write_text(
            "import pathlib, unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        path = pathlib.Path('feature.txt')\n"
            "        self.assertFalse(path.exists() and path.read_text() == 'broken')\n",
            encoding="utf-8",
        )
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.base = _git(self.repo, "rev-parse", "HEAD")
        (Path(self.repo) / "later.txt").write_text("after the pin", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "later")
        self.db = str(self.root / "benchmark.db")

    def task(self, task_id="t1", expected=("feature.txt", "other.txt")):
        return BenchmarkTask(
            id=task_id, goal=GOAL, base_commit=self.base, expected_files=tuple(expected),
            test_modules=("tests.test_ok",), tests_green_points=50,
            expected_files_points=30, evaluation_points=20, sha256="a" * 64,
        )

    def runner(self, worker, **kwargs):
        kwargs.setdefault("provider", "claude")
        kwargs.setdefault("price_db_path", str(self.root / "no-such.db"))
        kwargs.setdefault("read_rss", lambda: 2048)
        return BenchmarkRunner(
            repo_root=self.repo, db_path=self.db, worktrees_root=str(self.root / "wt"),
            test_python_executable=sys.executable, worker_factory=lambda _path: worker,
            experiment_log_dir=str(self.root / "logs"), **kwargs,
        )

    def rows(self, run_id):
        conn = init_db(self.db)
        try:
            return load_run(conn, run_id)
        finally:
            conn.close()

    def test_a_task_runs_on_the_pinned_commit_and_stores_its_metrics(self):
        worker = FakeWorker(["done"], usage={"input_tokens": 100, "output_tokens": 20})
        row = self.rows(self.runner(worker, model="m").run([self.task()]))["t1"]

        self.assertEqual(worker.saw_marker, [False])
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["evaluation_outcome"], "pass")
        self.assertEqual(row["tests_green"], 1)
        self.assertEqual(row["expected_files_touched"], 1)
        self.assertEqual(row["expected_files_total"], 2)
        self.assertEqual(row["score"], 85.0)  # 50 + 30/2 + 20
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (100, 20))
        self.assertEqual(row["peak_memory_bytes"], 2048)
        self.assertEqual((row["provider"], row["model"]), ("claude", "m"))
        self.assertEqual(row["base_commit"], self.base)
        self.assertGreater(row["runtime_ms"], 0)

    def test_repair_calls_count_towards_the_tokens(self):
        worker = FakeWorker(
            ["broken", "fixed"], usage={"input_tokens": 100, "output_tokens": 20}
        )
        row = self.rows(self.runner(worker).run([self.task()]))["t1"]

        self.assertEqual(len(worker.prompts), 2)
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (200, 40))

    def test_doing_nothing_earns_nothing_for_green_tests(self):
        row = self.rows(self.runner(FakeWorker([None])).run([self.task()]))["t1"]

        self.assertEqual(row["tests_green"], 0)
        self.assertEqual(row["score"], 0.0)

    def test_failed_tests_earn_only_the_touched_files(self):
        row = self.rows(
            self.runner(FakeWorker(["broken", "broken", "broken"])).run(
                [self.task(expected=("feature.txt",))]
            )
        )["t1"]

        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["score"], 30.0)
        self.assertIsNone(row["evaluation_outcome"])

    def test_unknown_usage_and_cost_stay_unknown(self):
        row = self.rows(self.runner(FakeWorker(["done"]), model="m").run([self.task()]))["t1"]

        self.assertIsNone(row["input_tokens"])
        self.assertIsNone(row["estimated_cost"])

    def test_cost_comes_from_the_production_price_table_without_changing_it(self):
        prices = str(self.root / "prices.db")
        conn = init_db(prices)
        conn.execute(
            "INSERT INTO model_prices (provider, model, input_price_per_million_tokens, "
            "output_price_per_million_tokens, currency, valid_from, source) "
            "VALUES ('claude', 'm', 2, 10, 'USD', '2020-01-01', 'test')"
        )
        conn.commit()
        conn.close()
        worker = FakeWorker(["done"], usage={"input_tokens": 1_000_000, "output_tokens": 100_000})

        row = self.rows(
            self.runner(worker, model="m", price_db_path=prices).run([self.task()])
        )["t1"]

        self.assertAlmostEqual(row["estimated_cost"], 3.0)
        self.assertEqual(row["currency"], "USD")

    def test_a_prompt_variant_reaches_the_worker_and_changes_the_hash(self):
        worker = FakeWorker(["done"])
        run_a = self.runner(FakeWorker(["done"])).run([self.task()])
        run_b = self.runner(worker, prompt_name="short", prompt_text="Keep it short.").run(
            [self.task()]
        )

        self.assertTrue(worker.prompts[0].endswith("Keep it short."))
        self.assertIn(GOAL, worker.prompts[0])
        row_a, row_b = self.rows(run_a)["t1"], self.rows(run_b)["t1"]
        self.assertEqual(row_b["prompt_name"], "short")
        self.assertNotEqual(row_a["prompt_sha256"], row_b["prompt_sha256"])

    def test_a_failing_agent_is_a_scored_row_and_the_next_task_still_runs(self):
        class Crashing:
            provider = "claude"

            def run(self, **_kwargs):
                raise RuntimeError("agent exploded")

        workers = iter([Crashing(), FakeWorker(["done"])])
        runner = self.runner(None)
        runner.worker_factory = lambda _path: next(workers)
        rows = self.rows(runner.run([self.task("t1"), self.task("t2")]))

        self.assertEqual(rows["t1"]["status"], "failed")
        self.assertEqual(rows["t1"]["score"], 0.0)
        self.assertIn("agent exploded", rows["t1"]["error"])
        self.assertEqual(rows["t2"]["status"], "succeeded")

    def test_a_task_that_cannot_start_is_an_error_row(self):
        broken = BenchmarkTask(**{**self.task().__dict__, "id": "t1", "goal": "  "})
        row = self.rows(self.runner(FakeWorker([])).run([broken]))["t1"]

        self.assertEqual(row["status"], "error")
        self.assertEqual(row["score"], 0.0)
        self.assertIsNone(row["experiment_id"])
        self.assertIn("goal must not be empty", row["error"])

    def test_worktree_and_branch_are_removed(self):
        run_id = self.runner(FakeWorker(["done"])).run([self.task()])

        self.assertTrue(self.rows(run_id)["t1"]["experiment_id"])
        self.assertEqual(_git(self.repo, "branch", "--list", "experiment/*"), "")
        self.assertEqual(list((self.root / "wt").iterdir()), [])
        self.assertEqual(_git(self.repo, "status", "--short"), "")

    def test_a_model_without_a_provider_is_refused(self):
        with self.assertRaises(ValueError):
            BenchmarkRunner(
                repo_root=self.repo, db_path=self.db, test_python_executable=sys.executable,
                model="m",
            )

    def test_runs_stay_out_of_the_production_database(self):
        with mock.patch("modules.benchmark.DEFAULT_DB_PATH", "/var/lib/nexus/agent.db"):
            self.assertEqual(default_benchmark_db_path(), "/var/lib/nexus/benchmark.db")


class ReportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = init_db(str(Path(tmp.name) / "benchmark.db"))
        self.addCleanup(self.conn.close)
        self.store("baseline", score=70, tokens=1000, cost=None, provider="claude")

    def store(self, run_id, score, tokens=500, cost=0.5, task_id="t1", sha="a" * 64,
              provider="codex", status="succeeded"):
        self.conn.execute(
            """INSERT INTO benchmark_runs
               (run_id, task_id, task_sha256, base_commit, provider, model, prompt_name,
                prompt_sha256, status, score, tests_green, expected_files_touched,
                expected_files_total, input_tokens, output_tokens, runtime_ms,
                estimated_cost, currency, peak_memory_bytes, started_at, ended_at)
               VALUES (?, ?, ?, 'c', ?, 'm', 'default', 'p', ?, ?, 1, 1, 1, ?, 0, 60000,
                       ?, ?, 104857600, 't', 't')""",
            (run_id, task_id, sha, provider, status, score, tokens, cost,
             "USD" if cost is not None else None),
        )
        self.conn.commit()

    def test_a_better_run(self):
        self.store("run", score=90)
        comparison = compare_runs(self.conn, "run", "baseline")
        report = format_report(comparison)

        self.assertEqual(comparison.verdict, VERDICT_BETTER)
        self.assertIn("Verdict: better", report)
        self.assertIn("| 70.0 -> 90.0 |", report)
        self.assertIn("| 1000 -> 500 |", report)
        self.assertIn("| 60 -> 60 |", report)
        self.assertIn("| 100 -> 100 |", report)
        self.assertIn("claude / m / prompt default", report)
        self.assertIn("codex / m / prompt default", report)

    def test_a_worse_run(self):
        self.store("run", score=40, status="failed")
        comparison = compare_runs(self.conn, "run", "baseline")

        self.assertEqual(comparison.verdict, VERDICT_WORSE)
        self.assertIn("| succeeded -> failed |", format_report(comparison))

    def test_a_tie_stays_a_tie(self):
        self.store("run", score=70)
        self.assertEqual(compare_runs(self.conn, "run", "baseline").verdict, VERDICT_EQUAL)

    def test_an_unknown_cost_is_never_reported_as_zero(self):
        self.store("run", score=90)
        report = format_report(compare_runs(self.conn, "run", "baseline"))

        self.assertIn("| n/a -> 0.5000 USD |", report)
        self.assertNotIn("0.0000", report)

    def test_a_total_with_a_gap_is_unknown(self):
        self.store("baseline", score=10, task_id="t2", cost=0.25)
        self.store("run", score=90)
        self.store("run", score=10, task_id="t2", cost=0.25)
        total = format_report(compare_runs(self.conn, "run", "baseline")).splitlines()[-1]

        self.assertIn("| n/a -> 0.7500 USD |", total)

    def test_a_missing_task_gives_no_verdict(self):
        self.store("baseline", score=10, task_id="t2")
        self.store("run", score=90)
        comparison = compare_runs(self.conn, "run", "baseline")

        self.assertEqual(comparison.verdict, VERDICT_NOT_COMPARABLE)
        self.assertIn("t2: only in baseline", format_report(comparison))

    def test_a_changed_task_gives_no_verdict(self):
        self.store("run", score=90, sha="b" * 64)
        comparison = compare_runs(self.conn, "run", "baseline")

        self.assertEqual(comparison.verdict, VERDICT_NOT_COMPARABLE)
        self.assertIn("t1: task definition changed", comparison.problems)

    def test_the_baseline_is_stored(self):
        self.assertIsNone(get_baseline(self.conn))
        set_baseline(self.conn, "baseline")
        self.assertEqual(get_baseline(self.conn), "baseline")

    def test_an_unknown_run_is_an_error(self):
        with self.assertRaises(BenchmarkError):
            set_baseline(self.conn, "nope")
        with self.assertRaises(BenchmarkError):
            compare_runs(self.conn, "nope", "baseline")


class FrozenTaskSetTests(unittest.TestCase):
    def test_the_set_is_small_and_valid(self):
        tasks = load_tasks()
        self.assertTrue(3 <= len(tasks) <= 10)
        for task in tasks:
            self.assertIsNone(_check_goal_policy(task.goal), task.id)

    def test_the_base_commits_exist(self):
        root = str(BENCHMARK_TASKS_DIR.parents[1])
        if _git(root, "rev-parse", "--is-shallow-repository") == "true":
            self.skipTest("shallow checkout has no history")
        for task in load_tasks():
            _git(root, "cat-file", "-e", f"{task.base_commit}^{{commit}}")

    def test_an_unknown_task_is_refused(self):
        with self.assertRaises(BenchmarkError):
            load_tasks(only=["nope"])

    def test_task_validation(self):
        good = (
            "---\nbase_commit: " + "a" * 40 + "\nexpected_files: a.py\n"
            "test_modules: tests.test_a\ntests_green_points: 50\n"
            "expected_files_points: 30\nevaluation_points: 20\n---\nGoal\n"
        )
        cases = {
            "short hash": good.replace("a" * 40, "abc123"),
            "shell in a module": good.replace("tests.test_a", "tests.a;rm"),
            "points": good.replace("points: 50", "points: 40"),
            "no goal": good.replace("---\nGoal\n", "---\n"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(good, encoding="utf-8")
            self.assertEqual(load_task(path).id, "task")
            for name, text in cases.items():
                path.write_text(text, encoding="utf-8")
                with self.subTest(name), self.assertRaises(BenchmarkError):
                    load_task(path)


class MeasuringTests(unittest.TestCase):
    def test_child_processes_count_towards_the_memory(self):
        own = process_tree_rss_bytes()
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; x = bytearray(64 * 1024 * 1024); time.sleep(30)"],
            stdin=subprocess.DEVNULL,
        )
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        for _ in range(200):
            total = process_tree_rss_bytes()
            if total > own + 32 * 1024 * 1024:
                break
            time.sleep(0.05)
        self.assertGreater(total, own + 32 * 1024 * 1024)

    def test_a_vanished_process_reads_as_unknown(self):
        self.assertIsNone(process_tree_rss_bytes(2**22 + 1))

    def test_codex_passes_a_chosen_model(self):
        command = CodexWorker(model="cheap")._build_command("t", "/w", "read-only")
        self.assertEqual(command[command.index("--model") + 1], "cheap")
        self.assertNotIn("--model", CodexWorker()._build_command("t", "/w", "read-only"))


class CliTests(unittest.TestCase):
    def test_a_report_without_a_baseline_says_so(self):
        import importlib.util

        script = BENCHMARK_TASKS_DIR.parents[1] / "scripts" / "run_benchmark.py"
        spec = importlib.util.spec_from_file_location("run_benchmark", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(module.main(["--db", f"{tmp}/b.db", "report", "x"]), 2)
            self.assertEqual(
                module.main(["--db", f"{tmp}/b.db", "report", "x", "--baseline", "y"]), 2
            )


class SchemaTests(unittest.TestCase):
    def test_init_db_creates_the_table_and_a_repeat_is_harmless(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/a.db"
            init_db(path).close()
            conn = init_db(path)
            try:
                conn.execute("SELECT run_id, peak_memory_bytes FROM benchmark_runs")
            finally:
                conn.close()
