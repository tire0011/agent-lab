"""#685: cache-aware admission - tokens the llama-server slot already holds are not charged."""

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

from modules import local_shadow_replay as lsr
from modules.coding_worker import WorkerResult
from modules.local_coding_worker import (
    _DIFF_CLOSING,
    _SR_CLOSING,
    _SYSTEM_PROMPT,
    MAX_GENERATION_TIME_SHARE,
    TIME_BUDGET_FRACTION,
    LocalCodingWorker,
    SentPrompt,
    TokenCounter,
    build_prompt_parts,
    common_prefix_tokens,
    local_edit_format,
    time_budget,
)
from modules.run_store import init_db

# Patches that do not apply, then one that does (diff and SEARCH/REPLACE form).
_BAD_DIFF = "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-not-there\n+x\n"
_GOOD_DIFF = "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-original\n+patched\n"
_BAD_SR = "value.txt\n<<<<<<< SEARCH\nnot-there\n=======\nx\n>>>>>>> REPLACE\n"
_GOOD_SR = "value.txt\n<<<<<<< SEARCH\noriginal\n=======\npatched\n>>>>>>> REPLACE\n"


def _closing() -> str:
    return _SR_CLOSING if local_edit_format() == "search_replace" else _DIFF_CLOSING


def _answers() -> tuple[str, str]:
    return (_BAD_SR, _GOOD_SR) if local_edit_format() == "search_replace" else (_BAD_DIFF, _GOOD_DIFF)


class _Server:
    """Fake llama-server on slot 0: whitespace tokenizer with ids, scripted answers, cache timings."""

    timeout = 300.0

    def __init__(self, responses: list[str], *, id_slot: int | None = 0, tokens_cached: int = 64,
                 n_ctx: int = 8192) -> None:
        self.responses = list(responses)
        self.id_slot = id_slot
        self.cache_prompt = True
        self.tokens_cached = tokens_cached
        self.n_ctx = n_ctx
        self.calls: list[dict] = []
        self.last_timings: dict | None = None

    async def context_size(self) -> int:
        return self.n_ctx

    async def tokenize(self, text: str) -> list[int]:
        # One token per 4 bytes, the id is the chunk's text hash: prefix-stable.
        data = text.encode()
        return [hash(data[i:i + 4]) for i in range(0, len(data), 4)]

    async def count_tokens(self, text: str) -> int:
        return len(await self.tokenize(text))

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 0, **_kw) -> str:
        self.calls.append({"prompt": prompt, "system": system, "max_tokens": max_tokens})
        self.last_timings = {"prompt_n": 100, "prompt_ms": 1000.0, "predicted_n": 10,
                             "predicted_ms": 1000.0, "tokens_cached": self.tokens_cached}
        return self.responses[min(len(self.calls), len(self.responses)) - 1]


class _Logger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, _phase, event, _message, **fields):
        self.events.append((event, fields))

    warning = info

    def fields(self, event: str) -> list[dict]:
        return [f for e, f in self.events if e == event]


class TimeBudgetTests(unittest.TestCase):
    RATES = (7.08, 2.61)  # prompt, generation tok/s as measured on the VM

    def test_zero_cached_prefix_is_the_cold_budget(self):
        for timeout, out, rates in ((300, 1024, self.RATES), (300, 4096, (50.0, 10.0)),
                                    (60, 512, (3.0, 1.0)), (None, 1024, self.RATES),
                                    (300, 1024, (None, 2.0))):
            self.assertEqual(time_budget(timeout, out, rates), time_budget(timeout, out, rates, 0))
        cold = time_budget(300, 1024, self.RATES)
        seconds = 300 * TIME_BUDGET_FRACTION
        generation = cold.output_tokens / 2.61
        self.assertEqual(int((seconds - generation) * 7.08), cold.prompt_tokens)

    def test_cached_prefix_grows_the_prompt_allowance_by_the_cached_amount(self):
        cold = time_budget(300, 1024, self.RATES)
        warm = time_budget(300, 1024, self.RATES, cached_prefix_tokens=5000)
        self.assertEqual(cold.prompt_tokens + 5000, warm.prompt_tokens)
        self.assertEqual((cold.output_tokens, cold.seconds), (warm.output_tokens, warm.seconds))
        # Bad values are no credit, never an error.
        for bad in (-5, True, float("nan"), None):
            self.assertEqual(cold, time_budget(300, 1024, self.RATES, cached_prefix_tokens=bad))

    def test_issue_scenario_6000_token_prompt_with_5000_cached(self):
        cold = time_budget(300, 1024, self.RATES)
        warm = time_budget(300, 1024, self.RATES, cached_prefix_tokens=5000)
        # Cold, a 6000-token prompt is ~7x over the prompt allowance ...
        self.assertLess(cold.prompt_tokens * 7, 6000)
        # ... warm, only its 1000 uncached tokens are charged at 7.08 tok/s.
        self.assertEqual(cold.prompt_tokens, warm.prompt_tokens - 5000)
        self.assertGreater(warm.prompt_tokens, 5000 + 800)
        # The issue's ">= 768 output tokens" cannot hold at 2.61 tok/s: 768 tokens
        # take ~294 s, more than the whole 240 s budget, whatever the cache does.
        # Output admission is a generation-rate limit; the cache does not move it.
        self.assertGreater(768 / 2.61, 300 * TIME_BUDGET_FRACTION)
        self.assertEqual(cold.output_tokens, warm.output_tokens)
        self.assertLessEqual(warm.output_tokens / 2.61,
                             300 * TIME_BUDGET_FRACTION * MAX_GENERATION_TIME_SHARE + 1)

    def test_cached_prefix_admits_a_richer_excerpt_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = [f"line {i}: filler = {i}" for i in range(400)]
            lines[200] = "target_name = 200"
            Path(tmp, "large.py").write_text("\n".join(lines) + "\n")
            client = _Server([])
            essential = "Change `target_name` in large.py"

            def build(cached: int):
                return asyncio.run(build_prompt_parts(
                    essential, "", tmp, client, max_output_tokens=512, n_ctx=100_000,
                    n_ctx_source="test", counter=TokenCounter(client), rates=(10.0, 10.0),
                    admission=False, cached_prefix_tokens=cached,
                ))

            cold, warm = build(0), build(5000)
        self.assertGreater(cold.file_level, 0)
        self.assertTrue(cold.time_budget_limited)
        self.assertEqual(0, warm.file_level)
        self.assertEqual(5000, warm.cached_prefix_tokens)
        self.assertEqual(cold.time_budget_tokens + 5000, warm.time_budget_tokens)
        self.assertEqual(0, cold.cached_prefix_tokens)


class CommonPrefixTests(unittest.TestCase):
    def test_token_ids_are_compared(self):
        server = _Server([])
        count = TokenCounter(server)
        shared = "a" * 400
        self.assertEqual(100, asyncio.run(common_prefix_tokens(server, count, shared + "xxxx", shared + "yyyy")))
        self.assertEqual(0, asyncio.run(common_prefix_tokens(server, count, "", shared)))

    def test_without_token_ids_the_text_prefix_is_under_counted(self):
        class Counter:
            source = "estimate"

            async def __call__(self, text):
                return -(-len(text.encode()) // 4)

        line = "x" * 39 + "\n"
        previous, current = line * 10 + "old tail", line * 10 + "new tail"
        reused = asyncio.run(common_prefix_tokens(object(), Counter(), previous, current))
        self.assertEqual(100 - 1, reused)
        self.assertEqual(0, asyncio.run(common_prefix_tokens(object(), Counter(), "abc", "abd")))


class SlotRuleTests(unittest.TestCase):
    PROMPT = "Repository context:\n" + "shared line\n" * 200

    def _reuse(self, sent: SentPrompt | None, *, slot: int | None = 0, system: str = _SYSTEM_PROMPT) -> int:
        worker = LocalCodingWorker(client=_Server([], id_slot=slot))
        worker._last_sent = sent
        return asyncio.run(worker._prefix_reuse(system, self.PROMPT + "round 2 feedback\n"))

    def _sent(self, **overrides) -> SentPrompt:
        values = dict(slot=0, system=_SYSTEM_PROMPT, prompt=self.PROMPT, tokens_cached=10, completed=True)
        values.update(overrides)
        return SentPrompt(**values)

    def test_same_slot_with_cached_tokens_is_credited(self):
        self.assertEqual(asyncio.run(_Server([]).count_tokens(self.PROMPT)), self._reuse(self._sent()))

    def test_other_slot_or_no_cache_report_gives_zero(self):
        self.assertEqual(0, self._reuse(self._sent(slot=1)))
        self.assertEqual(0, self._reuse(self._sent(tokens_cached=0)))
        self.assertEqual(0, self._reuse(self._sent(tokens_cached=None)))
        self.assertEqual(0, self._reuse(self._sent(), slot=None))
        self.assertEqual(0, self._reuse(self._sent(slot=None), slot=None))
        self.assertEqual(0, self._reuse(self._sent(completed=False)))
        self.assertEqual(0, self._reuse(self._sent(), system="other system"))
        self.assertEqual(0, self._reuse(None))


class WorkerRoundTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (("init", "-q"), ("config", "user.email", "t@example.com"), ("config", "user.name", "T")):
            self._git(*args)
        (self.repo / "value.txt").write_text("original\n")
        self._git("add", ".")
        self._git("commit", "-qm", "base")

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def _run(self, server: _Server, rates=(None, None)):
        log = _Logger()
        worker = LocalCodingWorker(client=server, max_tokens=1024, rates_path=self.root / "rates.json")
        with patch.object(worker.rates, "medians", return_value=rates):
            result = worker.run(task="Edit value.txt", working_dir=str(self.repo),
                                sandbox="workspace-write", experiment_logger=log)
        return worker, result, log

    def test_round_2_prompt_starts_with_round_1_up_to_the_feedback(self):
        server = _Server(list(_answers()))
        _worker, result, _log = self._run(server)
        self.assertTrue(result.success, result.error)
        first, second = (call["prompt"] for call in server.calls)
        self.assertTrue(first.endswith(_closing()))
        shared = first[:-len(_closing())]
        self.assertTrue(second.startswith(shared))
        self.assertTrue(second[len(shared):].startswith("Correction (round 2"))
        self.assertTrue(second.endswith(_closing()))

    def test_logs_carry_prefix_reuse(self):
        server = _Server(list(_answers()))
        worker, result, log = self._run(server)
        self.assertTrue(result.success, result.error)
        calls = log.fields("local_model_call")
        self.assertEqual(2, len(calls))
        self.assertEqual((0, 0.0), (calls[0]["prefix_reuse_tokens"], calls[0]["prefix_reuse_ratio"]))
        shared = asyncio.run(server.count_tokens(server.calls[0]["prompt"][:-len(_closing())]))
        self.assertGreaterEqual(calls[1]["prefix_reuse_tokens"], shared - 1)  # boundary chunk
        self.assertGreater(calls[1]["prefix_reuse_ratio"], 0.0)
        self.assertLessEqual(calls[1]["prefix_reuse_ratio"], 1.0)
        rounds = log.fields("local_patch_round")
        self.assertEqual([r["prefix_reuse_tokens"] for r in rounds],
                         [c["prefix_reuse_tokens"] for c in calls])
        self.assertIn("prefix_reuse_ratio", rounds[1])
        budget, = log.fields("local_prompt_budget")
        self.assertIn("prefix_reuse_tokens", budget)
        self.assertIn("prefix_reuse_ratio", budget)
        metrics, = log.fields("local_run_metrics")
        self.assertEqual(calls[1]["prefix_reuse_tokens"], metrics["prefix_reuse_tokens"])
        self.assertGreater(metrics["prefix_reuse_ratio"], 0.0)
        self.assertEqual(metrics, worker.last_run_metrics)

    def test_no_reuse_on_another_slot_or_without_cache_report(self):
        for server in (_Server(list(_answers()), tokens_cached=0), _Server(list(_answers()), id_slot=None)):
            self._git("checkout", "--", "value.txt")
            _worker, result, log = self._run(server)
            self.assertTrue(result.success, result.error)
            self.assertEqual([0, 0], [c["prefix_reuse_tokens"] for c in log.fields("local_model_call")])

    def test_warm_round_2_budget_keeps_more_handover(self):
        from modules.experiment_handover import HANDOVER_HEADING

        server = _Server([])
        worker = LocalCodingWorker(client=server, max_tokens=512)
        handover = f"{HANDOVER_HEADING}\n" + "".join(f"note {i}: earlier finding\n" for i in range(400))
        task = "Edit value.txt\n\n" + handover
        rates = (10.0, 10.0)
        with patch.object(worker.rates, "medians", return_value=rates):
            from modules.local_coding_worker import build_prompt
            plan = asyncio.run(build_prompt(task, str(self.repo), server, max_output_tokens=512,
                                            rates=rates, admission=False))
        self.assertTrue(plan.handover_cut)
        cold, credited_cold = asyncio.run(worker._feedback_prompt(plan, 2, "does not apply", "x"))
        self.assertEqual(0, credited_cold)  # nothing sent yet: no credit
        worker._last_sent = SentPrompt(0, _SYSTEM_PROMPT, plan.prompt, 50, True)
        warm, credited = asyncio.run(worker._feedback_prompt(plan, 2, "does not apply", "x"))
        self.assertGreater(credited, 0)
        self.assertGreater(warm.handover_tokens_kept, cold.handover_tokens_kept)
        self.assertGreater(warm.prompt_tokens, plan.budget_tokens)
        context_budget = plan.n_ctx - 512 - 256
        self.assertLessEqual(warm.prompt_tokens, context_budget)


class ReplayRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo, self.db = str(root / "repo"), str(root / "agent.db")
        env = patch.dict(os.environ, {lsr.ENV_WORKTREE_DIR: str(root / "worktrees")})
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(self.repo)

        def sh(*args):
            return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True,
                                  text=True).stdout.strip()

        sh("init", "-q", "-b", "main")
        sh("config", "user.email", "t@example.com")
        sh("config", "user.name", "T")
        Path(self.repo, "calc.py").write_text("def add(a, b):\n    return 0\n")
        sh("add", "-A")
        sh("commit", "-qm", "base")
        base = sh("rev-parse", "HEAD")
        Path(self.repo, "calc.py").write_text("def add(a, b):\n    return a + b\n")
        os.makedirs(Path(self.repo, "tests"))
        Path(self.repo, "tests", "test_calc.py").write_text(
            "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n")
        sh("add", "-A")
        sh("commit", "-qm", "merged (#42)")
        merged = sh("rev-parse", "HEAD")
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

    def test_replay_row_carries_prefix_reuse(self):
        class Worker:
            def run(self, *, working_dir, experiment_logger, **_):
                Path(working_dir, "calc.py").write_text("def add(a, b):\n    return a + b\n")
                experiment_logger.info("x", "local_run_metrics", "m", samples_setting=1,
                                       prefix_reuse_tokens=4200, prefix_reuse_ratio=0.7)
                return WorkerResult(success=True, exit_code=0, stdout="", stderr="", runtime_ms=1,
                                    timed_out=False, stdout_truncated=False, stderr_truncated=False,
                                    thread_id=None, final_message=None, usage=None, task="t",
                                    working_dir=working_dir, sandbox="workspace-write", error=None,
                                    provider="local")

        class Client:
            model = "fake"

        row = lsr.replay("exp-1", repo_root=self.repo, db_path=self.db, variant="baseline",
                         client=Client(), worker_factory=lambda _c: Worker())
        self.assertEqual((4200, 0.7), (row["prefix_reuse_tokens"], row["prefix_reuse_ratio"]))
        conn = sqlite3.connect(self.db)
        stored = json.loads(conn.execute("SELECT metrics_json FROM local_replays").fetchone()[0])
        conn.close()
        self.assertEqual(4200, stored["prefix_reuse_tokens"])


if __name__ == "__main__":
    unittest.main()
