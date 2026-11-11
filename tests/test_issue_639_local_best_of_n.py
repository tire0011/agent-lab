"""#639: best-of-N local patch sampling, picked by apply/compile/tests, and its cost.

A fake local model returns scripted answers per temperature; a temporary git
repository holds the code and the named tests, which run for real with pytest.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules import local_coding_worker as lcw
from modules import local_shadow_replay as lsr
from modules.coding_provider import build_worker
from modules.local_coding_worker import (
    ENV_LOCAL_SAMPLES,
    ENV_SAMPLE_TEMPERATURES,
    MAX_LOCAL_SAMPLES,
    LocalCodingWorker,
    build_prompt,
    failing_test_count,
    local_samples,
    sample_schedule,
    sample_temperatures,
    check_in_worktree,
)
from modules.local_run_metrics import (
    RunMetrics,
    call_record,
    find_server_pid,
    process_cpu_seconds,
    process_memory_kb,
)
from modules.run_store import init_db

BASE = "A = 0\nB = 0\nC = 0\n"
TESTS = (
    "import mod\n\n\n"
    "def test_a():\n    assert mod.A == 1\n\n\n"
    "def test_b():\n    assert mod.B == 1\n\n\n"
    "def test_c():\n    assert mod.C == 1\n"
)
ONE = "--- a/mod.py\n+++ b/mod.py\n@@ -1,3 +1,3 @@\n-A = 0\n+A = 1\n B = 0\n C = 0\n"
TWO = "--- a/mod.py\n+++ b/mod.py\n@@ -1,3 +1,3 @@\n-A = 0\n-B = 0\n+A = 1\n+B = 1\n C = 0\n"
ALL = "--- a/mod.py\n+++ b/mod.py\n@@ -1,3 +1,3 @@\n-A = 0\n-B = 0\n-C = 0\n+A = 1\n+B = 1\n+C = 1\n"
BROKEN = "--- a/mod.py\n+++ b/mod.py\n@@ -1,3 +1,3 @@\n-A = 0\n+A = (\n B = 0\n C = 0\n"
NOT_APPLYING = "--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n-nothing here\n+x\n"
NAMED = ("tests/test_mod.py",)


class FakeModel:
    """Fake llama-server: n_ctx, 4 bytes per token, answers scripted per temperature.

    Every call records the real tree's ``mod.py`` at that moment, so a test can
    prove a losing candidate never reached it.
    """

    def __init__(self, script: dict[float, list[str]], repo: Path | None = None, *, n_ctx: int = 16384):
        self.script = {t: list(a) for t, a in script.items()}
        self.repo = repo
        self.n_ctx = n_ctx
        self.calls: list[dict] = []
        self.last_timings = None

    async def context_size(self) -> int:
        return self.n_ctx

    async def count_tokens(self, text: str) -> int:
        return -(-len(text.encode()) // 4)

    async def complete(self, prompt: str, *, system: str = "", temperature: float = 0.0,
                       max_tokens: int = 0, seed: int | None = None) -> str:
        tree = (self.repo / "mod.py").read_text() if self.repo else None
        self.calls.append({"prompt": prompt, "system": system, "temperature": temperature,
                           "max_tokens": max_tokens, "seed": seed, "tree": tree})
        answers = self.script.get(temperature) or [""]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        self.last_timings = {"prompt_n": 100, "prompt_ms": 50.0, "predicted_n": 20, "predicted_ms": 40.0}
        return answer


class StrictModel(FakeModel):
    """A client whose ``complete`` takes no ``seed`` and no other extras (today's signature)."""

    async def complete(self, prompt: str, *, system: str = "", temperature: float = 0.0,  # type: ignore[override]
                       max_tokens: int = 0) -> str:
        return await super().complete(prompt, system=system, temperature=temperature, max_tokens=max_tokens)


class Log:
    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    def info(self, _phase, event, message, **fields):
        self.events.append((event, message, fields))

    warning = info

    def of(self, event: str) -> list[dict]:
        return [fields for name, _msg, fields in self.events if name == event]


class RepoCase(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        for name in (ENV_LOCAL_SAMPLES, ENV_SAMPLE_TEMPERATURES):
            os.environ.pop(name, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.copies = self.root / "copies"
        self.copies.mkdir()
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        (self.repo / "mod.py").write_text(BASE)
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_mod.py").write_text(TESTS)
        self._git("add", ".")
        self._git("commit", "-qm", "base")

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout

    def worker(self, model, samples=1, temperatures=None) -> LocalCodingWorker:
        return LocalCodingWorker(client=model, max_tokens=1024, samples=samples,
                                 sample_temperatures=temperatures, sample_tmp_root=str(self.copies))

    def patch_rounds(self, worker, log, tests=NAMED):
        plan = asyncio.run(build_prompt("Set A, B and C in mod.py to 1", str(self.repo), worker.client,
                                        max_output_tokens=1024))
        return worker._patch_rounds(plan, str(self.repo), 120, log, "implementation", tests=tests)


class ConfigTests(unittest.TestCase):
    def test_env_defaults_and_parsing(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(local_samples(), 1)
            self.assertEqual(sample_temperatures(), (0.4, 0.7, 1.0))
        with patch.dict(os.environ, {ENV_LOCAL_SAMPLES: "4", ENV_SAMPLE_TEMPERATURES: "0.2, x, 0.9, 5"}):
            self.assertEqual(local_samples(), 4)
            self.assertEqual(sample_temperatures(), (0.2, 0.9))
        for raw, expected in (("0", 1), ("-3", 1), ("junk", 1), ("99", MAX_LOCAL_SAMPLES)):
            with patch.dict(os.environ, {ENV_LOCAL_SAMPLES: raw}):
                self.assertEqual(local_samples(), expected, raw)

    def test_schedule_is_greedy_first_then_the_spread_with_distinct_seeds(self):
        self.assertEqual(sample_schedule(1), [(0.0, None)])
        schedule = sample_schedule(5, (0.4, 0.7, 1.0))
        self.assertEqual([t for t, _ in schedule], [0.0, 0.4, 0.7, 1.0, 0.4])
        seeds = [s for _, s in schedule[1:]]
        self.assertEqual(len(set(seeds)), 4)
        self.assertIsNone(schedule[0][1])

    def test_production_factories_read_the_env(self):
        with patch.dict(os.environ, {ENV_LOCAL_SAMPLES: "3", ENV_SAMPLE_TEMPERATURES: "0.5"}):
            worker = build_worker("local", client=FakeModel({}))
            self.assertEqual((worker.samples, worker.sample_temperatures), (3, (0.5,)))
            replay_worker = lsr.default_worker_factory(FakeModel({}))
            self.assertEqual(replay_worker.samples, 3)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(build_worker("local", client=FakeModel({})).samples, 1)

    def test_client_sends_seed_only_when_given(self):
        from modules.llama_cpp_client import LlamaCppClient

        sent = []

        class Capturing(LlamaCppClient):
            async def chat_completion(self, messages, temperature=0.0, max_tokens=1024, **kwargs):
                sent.append({"temperature": temperature, **kwargs})
                return {"choices": [{"message": {"content": "ok"}}]}

        client = Capturing(base_url="http://127.0.0.1:1/v1")
        asyncio.run(client.complete("p"))
        asyncio.run(client.complete("p", temperature=0.7, seed=641))
        self.assertEqual(sent, [{"temperature": 0.0}, {"temperature": 0.7, "seed": 641}])

    def test_failing_test_count_reads_pytest_and_unittest_summaries(self):
        self.assertEqual(failing_test_count("2 failed, 1 passed in 0.1s", NAMED), 2)
        self.assertEqual(failing_test_count("1 failed, 1 error in 0.1s", NAMED), 2)
        self.assertEqual(failing_test_count("FAILED (failures=3, errors=1)", NAMED), 4)
        self.assertEqual(failing_test_count("Traceback ...", ("a", "b")), 2)


class SingleSampleTests(RepoCase):
    def test_n1_calls_and_result_are_unchanged(self):
        model = StrictModel({0.0: [ONE, ALL]}, self.repo)
        worker = self.worker(model)
        log = Log()
        ok, output, reason = self.patch_rounds(worker, log)

        self.assertEqual((ok, output, reason), (True, ALL, ""))
        # Same two greedy calls as before #639: temperature 0.0, no seed, no extras.
        self.assertEqual([c["temperature"] for c in model.calls], [0.0, 0.0])
        self.assertEqual([c["seed"] for c in model.calls], [None, None])
        # The second prompt is the correction round with the first diff as feedback.
        self.assertIn("Correction (round 2", model.calls[1]["prompt"])
        self.assertIn("+A = 1", model.calls[1]["prompt"])
        self.assertEqual([r["outcome"] for r in log.of("local_patch_round")], ["tests_failed", "applied"])
        # N=1 never makes throwaway copies: the patch is checked in place, as before.
        self.assertEqual(list(self.copies.iterdir()), [])
        self.assertEqual((self.repo / "mod.py").read_text(), "A = 1\nB = 1\nC = 1\n")
        self.assertNotIn("sample", log.of("local_model_call")[0])

        result, = log.of("local_samples_result")
        self.assertEqual((result["samples_setting"], result["samples_requested"], result["samples_applied"],
                          result["samples_compiled"], result["samples_passed"]), (1, 2, 2, 2, 1))
        self.assertEqual((result["winner_index"], result["winner_temperature"], result["winner_round"],
                          result["winner_reason"]), (0, 0.0, 2, "tests"))

    def test_n1_default_through_run_matches_explicit_n1(self):
        outcomes = []
        for samples in (None, 1):
            model = StrictModel({0.0: [BROKEN, ALL]})
            repo = self.root / f"run-{samples}"
            subprocess.run(["git", "clone", "-q", str(self.repo), str(repo)], check=True)
            kwargs = {} if samples is None else {"samples": samples}
            worker = LocalCodingWorker(client=model, max_tokens=1024, **kwargs)
            result = worker.run(task="Set A, B and C in mod.py to 1", working_dir=str(repo),
                                sandbox="workspace-write", experiment_logger=Log())
            outcomes.append((result.success, result.stdout, result.error,
                             [(c["prompt"], c["temperature"], c["max_tokens"]) for c in model.calls]))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertTrue(outcomes[0][0])


class BestOfNTests(RepoCase):
    def test_candidate_worktree_preserves_git_state_untracked_files_and_local_dependencies(self):
        # Simulate an earlier subtask, a generated untracked acceptance test,
        # and a repository-local installation ignored by Git.
        (self.repo / "earlier.txt").write_text("seen\n")
        (self.repo / ".gitignore").write_text("node_modules/\n")
        self._git("add", ".gitignore")
        self._git("commit", "-qm", "ignore dependencies")
        dependencies = self.repo / "node_modules"
        dependencies.mkdir()
        (dependencies / "marker").write_text("installed\n")
        acceptance = self.repo / "tests" / "test_candidate_environment.py"
        acceptance.write_text(
            "import pathlib, subprocess\n\n"
            "def test_environment():\n"
            "    assert subprocess.run(['git', 'rev-parse', '--is-inside-work-tree'], "
            "capture_output=True, text=True).stdout.strip() == 'true'\n"
            "    assert pathlib.Path('earlier.txt').read_text() == 'seen\\n'\n"
            "    assert pathlib.Path('node_modules/marker').read_text() == 'installed\\n'\n"
            "    assert pathlib.Path('mod.py').read_text().startswith('A = 1')\n"
        )

        check = check_in_worktree(
            ONE, str(self.repo), 120, tests=("tests/test_candidate_environment.py",),
            tmp_root=str(self.copies),
        )
        self.assertTrue(check.passed, check.reason)
        self.assertEqual((self.repo / "mod.py").read_text(), BASE)
        self.assertEqual(list(self.copies.iterdir()), [])
        worktrees = self._git("worktree", "list", "--porcelain")
        self.assertNotIn("nexus-local-sample-", worktrees)

    def test_exception_halfway_cleans_up_and_a_retry_succeeds(self):
        with patch.object(lcw, "run_named_tests_detail", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                check_in_worktree(ALL, str(self.repo), 120, tests=NAMED,
                                  tmp_root=str(self.copies))
        self.assertEqual(list(self.copies.iterdir()), [])
        self.assertNotIn("nexus-local-sample-", self._git("worktree", "list", "--porcelain"))

        # Running the same step again must neither collide with stale metadata
        # nor observe the failed candidate's patch.
        check = check_in_worktree(ALL, str(self.repo), 120, tests=NAMED,
                                  tmp_root=str(self.copies))
        self.assertTrue(check.passed, check.reason)
        self.assertEqual((self.repo / "mod.py").read_text(), BASE)
        self.assertEqual(list(self.copies.iterdir()), [])

    def test_timeout_during_setup_removes_partial_worktree(self):
        real_overlay = lcw._overlay_working_state

        def interrupted(source, target, timeout):
            real_overlay(source, target, timeout)
            raise subprocess.TimeoutExpired("overlay", timeout)

        with patch.object(lcw, "_overlay_working_state", side_effect=interrupted):
            check = check_in_worktree(ALL, str(self.repo), 120, tests=NAMED,
                                      tmp_root=str(self.copies))
        self.assertEqual(check.outcome, "timed_out")
        self.assertEqual(list(self.copies.iterdir()), [])
        self.assertNotIn("nexus-local-sample-", self._git("worktree", "list", "--porcelain"))

    def test_first_passing_candidate_wins_and_the_third_is_never_requested(self):
        model = FakeModel({0.0: [TWO], 0.7: [ALL], 1.0: [ALL]}, self.repo)
        worker = self.worker(model, samples=3, temperatures=(0.7, 1.0))
        log = Log()
        ok, output, _reason = self.patch_rounds(worker, log)

        self.assertTrue(ok)
        self.assertEqual(output, ALL)
        self.assertEqual([c["temperature"] for c in model.calls], [0.0, 0.7])
        self.assertEqual(model.calls[0]["seed"], None)
        self.assertIsNotNone(model.calls[1]["seed"])
        # The real tree held only the base until the winner was applied.
        self.assertEqual([c["tree"] for c in model.calls], [BASE, BASE])
        self.assertEqual((self.repo / "mod.py").read_text(), "A = 1\nB = 1\nC = 1\n")
        self.assertEqual(list(self.copies.iterdir()), [])

        result, = log.of("local_samples_result")
        self.assertEqual((result["samples_requested"], result["samples_applied"], result["samples_compiled"],
                          result["samples_passed"]), (2, 2, 2, 1))
        self.assertEqual((result["winner_index"], result["winner_temperature"], result["winner_reason"]),
                         (1, 0.7, "tests"))
        self.assertEqual(result["local_tokens"], 2 * 120)
        self.assertGreater(result["local_seconds"], 0)
        samples = log.of("local_patch_sample")
        self.assertEqual([s["outcome"] for s in samples], ["tests_failed", "applied"])
        self.assertEqual(samples[0]["failing_tests"], 1)
        self.assertEqual([s["temperature"] for s in samples], [0.0, 0.7])

    def test_without_a_pass_the_compiled_candidate_with_fewest_failures_feeds_the_correction(self):
        model = FakeModel({0.0: [ONE, ALL], 0.4: [TWO], 0.7: [BROKEN]}, self.repo)
        worker = self.worker(model, samples=3)
        log = Log()
        ok, _output, _reason = self.patch_rounds(worker, log)

        self.assertTrue(ok)
        self.assertEqual([c["temperature"] for c in model.calls], [0.0, 0.4, 0.7, 0.0])
        correction = model.calls[3]["prompt"]
        self.assertIn("Correction (round 2", correction)
        self.assertIn("+B = 1", correction)  # TWO's diff (1 failing test) ...
        self.assertNotIn("+A = (", correction)  # ... not the broken one
        first_round = log.of("local_patch_round")[0]
        self.assertEqual((first_round["outcome"], first_round["best_index"], first_round["best_failing_tests"]),
                         ("tests_failed", 1, 1))
        samples = log.of("local_patch_sample")
        self.assertEqual([s["failing_tests"] for s in samples[:3]], [2, 1, 0])
        self.assertEqual([s["compiled"] for s in samples[:3]], [True, True, False])
        # Losing candidates never touched the real tree; their copies are gone.
        self.assertTrue(all(c["tree"] == BASE for c in model.calls))
        self.assertEqual(list(self.copies.iterdir()), [])
        self.assertEqual(self._git("status", "--porcelain"), " M mod.py\n")

        result, = log.of("local_samples_result")
        self.assertEqual((result["samples_requested"], result["samples_applied"], result["samples_compiled"],
                          result["samples_passed"], result["rounds"]), (4, 4, 3, 1, 2))
        self.assertEqual((result["winner_index"], result["winner_temperature"], result["winner_round"]),
                         (0, 0.0, 2))

    def test_all_rounds_fail_leaves_the_tree_untouched_and_logs_no_winner(self):
        model = FakeModel({0.0: [NOT_APPLYING], 0.4: [BROKEN]}, self.repo)
        worker = self.worker(model, samples=2)
        log = Log()
        ok, output, reason = self.patch_rounds(worker, log)

        self.assertFalse(ok)
        self.assertEqual(output, "")
        self.assertIn("py_compile", reason)  # the broken-but-applied one got further
        self.assertEqual(len(model.calls), 2 * lcw.MAX_PATCH_ROUNDS)
        self.assertEqual(self._git("status", "--porcelain"), "")
        self.assertEqual(list(self.copies.iterdir()), [])
        result, = log.of("local_samples_result")
        self.assertEqual((result["samples_requested"], result["samples_applied"], result["samples_compiled"],
                          result["samples_passed"], result["winner_index"], result["winner_temperature"]),
                         (6, 3, 0, 0, None, None))

    def test_seed_is_left_out_for_clients_that_do_not_take_it(self):
        model = StrictModel({0.0: [ONE], 0.4: [ALL]}, self.repo)
        ok, _out, _reason = self.patch_rounds(self.worker(model, samples=2), Log())
        self.assertTrue(ok)
        self.assertEqual([c["temperature"] for c in model.calls], [0.0, 0.4])

    def test_run_logs_run_metrics_with_sample_counts(self):
        model = FakeModel({0.0: [BROKEN], 0.4: [ALL]}, self.repo)
        worker = self.worker(model, samples=2)
        log = Log()
        result = worker.run(task="Set A, B and C in mod.py to 1", working_dir=str(self.repo),
                            sandbox="workspace-write", experiment_logger=log)
        self.assertTrue(result.success, result.error)
        metrics, = log.of("local_run_metrics")
        self.assertEqual(metrics, worker.last_run_metrics)
        self.assertEqual((metrics["samples_setting"], metrics["samples_requested"],
                          metrics["samples_passed"], metrics["patch_samples"]), (2, 2, 1, 2))
        self.assertEqual((metrics["tokens_in"], metrics["tokens_out"]), (200, 40))
        self.assertEqual(len(metrics["sample_wall_seconds"]), 2)
        self.assertEqual(metrics["winners"], [{"index": 1, "temperature": 0.4, "round": 1, "reason": "compile"}])
        self.assertEqual([s["outcome"] for s in metrics["samples"]], ["compile_failed", "applied"])
        for key in ("wall_seconds", "llama_server_cpu_seconds", "load_average", "llama_server_peak_rss_kb",
                    "tokens_per_second"):
            self.assertIn(key, metrics)
        message = next(m for e, m, _f in log.events if e == "local_run_metrics")
        self.assertIn("samples N=2", message)


class RunMetricsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.proc = Path(tmp.name)
        (self.proc / "1").mkdir()
        (self.proc / "1" / "comm").write_text("systemd\n")
        (self.proc / "1" / "cmdline").write_bytes(b"/sbin/init\0")
        self.pid = self.proc / "4242"
        self.pid.mkdir()
        (self.pid / "comm").write_text("llama-server\n")
        (self.pid / "cmdline").write_bytes(b"/opt/llama-server\0-m\0model.gguf\0")
        self.set_proc(utime=100, stime=50, rss=1000, hwm=5000)

    def set_proc(self, *, utime, stime, rss, hwm):
        fields = ["S"] + ["0"] * 10 + [str(utime), str(stime)] + ["0"] * 30
        (self.pid / "stat").write_text(f"4242 (llama server) {' '.join(fields)}\n")
        (self.pid / "status").write_text(f"Name:\tllama-server\nVmHWM:\t{hwm} kB\nVmRSS:\t{rss} kB\n")

    def test_process_readers(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(find_server_pid(self.proc), 4242)
        ticks = os.sysconf("SC_CLK_TCK")
        self.assertAlmostEqual(process_cpu_seconds(4242, self.proc), 150 / ticks)
        self.assertEqual(process_memory_kb(4242, self.proc), (1000, 5000))
        self.assertIsNone(process_cpu_seconds(9999, self.proc))

    def test_run_metrics_measure_cpu_delta_peak_rss_and_tokens(self):
        with patch.dict(os.environ, {}, clear=True):
            metrics = RunMetrics.start(self.proc)
        self.set_proc(utime=100 + 2 * os.sysconf("SC_CLK_TCK"), stime=50, rss=3000, hwm=6000)
        metrics.record_call(call_record("patch", "ok", 2.0,
                                        {"prompt_n": 80, "tokens_cached": 20, "predicted_n": 40}))
        self.set_proc(utime=100 + 2 * os.sysconf("SC_CLK_TCK"), stime=50, rss=2000, hwm=6000)
        fields = metrics.finish()
        self.assertEqual(fields["llama_server_pid"], 4242)
        self.assertAlmostEqual(fields["llama_server_cpu_seconds"], 2.0)
        self.assertEqual(fields["llama_server_peak_rss_kb"], 3000)
        self.assertEqual(fields["llama_server_hwm_kb"], 6000)
        self.assertEqual((fields["tokens_in"], fields["tokens_out"]), (100, 40))
        self.assertEqual(fields["tokens_per_second"], 20.0)
        self.assertEqual(fields["sample_wall_seconds"], [2.0])

    def test_no_server_process_gives_none_fields(self):
        empty = self.proc / "empty"
        empty.mkdir()
        with patch.dict(os.environ, {}, clear=True):
            fields = RunMetrics.start(empty).finish()
        self.assertIsNone(fields["llama_server_pid"])
        self.assertIsNone(fields["llama_server_cpu_seconds"])
        self.assertIsNone(fields["llama_server_peak_rss_kb"])


def _sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ReplayStoresMetricsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo, self.db = str(root / "repo"), str(root / "agent.db")
        env = patch.dict(os.environ, {lsr.ENV_WORKTREE_DIR: str(root / "worktrees")})
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(self.repo)
        _sh(self.repo, "init", "-q", "-b", "main")
        _sh(self.repo, "config", "user.email", "t@example.com")
        _sh(self.repo, "config", "user.name", "T")
        Path(self.repo, "calc.py").write_text("def add(a, b):\n    return 0\n")
        _sh(self.repo, "add", "-A")
        _sh(self.repo, "commit", "-qm", "base")
        base = _sh(self.repo, "rev-parse", "HEAD")
        Path(self.repo, "calc.py").write_text("def add(a, b):\n    return a + b\n")
        os.makedirs(Path(self.repo, "tests"))
        Path(self.repo, "tests", "test_calc.py").write_text(
            "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n")
        _sh(self.repo, "add", "-A")
        _sh(self.repo, "commit", "-qm", "merged (#42)")
        merged = _sh(self.repo, "rev-parse", "HEAD")
        init_db(self.db).close()
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("INSERT INTO experiments (id, goal, status, created_at, implementation_provider, "
                         "base_commit_sha) VALUES ('exp-1', 'GitHub Issue #42: fix', 'succeeded', "
                         "'2026-11-01T00:00:00+00:00', 'codex', ?)", (base,))
            conn.execute("INSERT INTO experiment_adoptions (id, experiment_id, status, ended_at, "
                         "integrated_commit_sha, created_at, updated_at) VALUES ('ad', 'exp-1', 'adopted', "
                         "'2026-11-01', ?, '2026-11-01', '2026-11-01')", (merged,))
        conn.close()

    def test_replay_row_carries_samples_and_cost(self):
        class Worker:
            def run(self, *, working_dir, experiment_logger, **_):
                Path(working_dir, "calc.py").write_text("def add(a, b):\n    return a + b\n")
                experiment_logger.info("x", "local_run_metrics", "m", samples_setting=4, samples_requested=3,
                                       samples_passed=1, llama_server_cpu_seconds=12.5, load_average=1.5,
                                       llama_server_peak_rss_kb=2048, tokens_per_second=7.5)
                from modules.coding_worker import WorkerResult
                return WorkerResult(success=True, exit_code=0, stdout="", stderr="", runtime_ms=1,
                                    timed_out=False, stdout_truncated=False, stderr_truncated=False,
                                    thread_id=None, final_message=None, usage=None, task="t",
                                    working_dir=working_dir, sandbox="workspace-write", error=None,
                                    provider="local")

        class Client:
            model = "fake"

        row = lsr.replay("exp-1", repo_root=self.repo, db_path=self.db, variant="samples4",
                         client=Client(), worker_factory=lambda _c: Worker())
        self.assertEqual((row["samples"], row["samples_requested"], row["samples_passed"]), (4, 3, 1))
        self.assertEqual((row["cpu_seconds"], row["peak_rss_kb"], row["tokens_per_second"]), (12.5, 2048, 7.5))
        conn = sqlite3.connect(self.db)
        stored = json.loads(conn.execute("SELECT metrics_json FROM local_replays").fetchone()[0])
        conn.close()
        self.assertEqual(stored["samples_setting"], 4)
        stats, = lsr.variant_summary(db_path=self.db)["variants"]
        self.assertEqual((stats["samples"], stats["median_cpu_seconds"], stats["peak_rss_kb"]), ([4], 12.5, 2048))
        self.assertIsNotNone(stats["median_seconds"])


if __name__ == "__main__":
    unittest.main()
