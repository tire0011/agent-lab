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
    FEEDBACK_RESERVE_TOKENS,
    MAX_GENERATION_TIME_SHARE,
    TIME_BUDGET_FRACTION,
    LocalCodingWorker,
    SentPrompt,
    Subtask,
    TokenCounter,
    build_feedback_prompt,
    build_prompt,
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

    def test_warm_round_2_budget_keeps_the_handover_and_more_previous_answer(self):
        server = _Server([])
        worker = LocalCodingWorker(client=server, max_tokens=512)
        task = "Edit value.txt\n\n" + _handover(400)
        rates = (10.0, 10.0)
        plan = asyncio.run(build_prompt(task, str(self.repo), server, max_output_tokens=512,
                                        rates=rates, admission=False))
        self.assertTrue(plan.handover_cut)
        previous = "".join(f"value.txt line {i} of a long previous answer\n" for i in range(600))
        cold, credited_cold = asyncio.run(worker._feedback_prompt(plan, 2, "does not apply", previous))
        self.assertEqual(0, credited_cold)  # nothing sent yet: no credit
        worker._last_sent = SentPrompt(0, _SYSTEM_PROMPT, plan.prompt, 50, True)
        warm, credited = asyncio.run(worker._feedback_prompt(plan, 2, "does not apply", previous))
        self.assertGreater(credited, 0)
        shared = _shared(plan.prompt)
        # The warm budget never buys more handover: that would move the point
        # where round 2 departs from round 1 and lose the cached prefix.
        for fb in (cold, warm):
            self.assertTrue(fb.prompt.startswith(shared))
            self.assertEqual(plan.handover_tokens_kept, fb.handover_tokens_kept)
        self.assertTrue(cold.diff_cut)
        self.assertGreater(len(warm.prompt), len(cold.prompt))  # more of the previous answer
        self.assertGreater(warm.prompt_tokens, plan.budget_tokens)
        context_budget = plan.n_ctx - 512 - 256
        self.assertLessEqual(warm.prompt_tokens, context_budget)


def _handover(lines: int, label: str = "note") -> str:
    from modules.experiment_handover import HANDOVER_HEADING

    return f"{HANDOVER_HEADING}\nFIRST-FINDING\n" + "".join(
        f"{label} {i}: an earlier finding about the code\n" for i in range(lines))


def _shared(round_1_prompt: str) -> str:
    """Round 1's prompt up to where a correction round inserts its feedback."""
    assert round_1_prompt.endswith(_closing())
    return round_1_prompt[:-len(_closing())]


class _NonAdditiveCounter:
    """Counts like the server but charges every feedback section heavily.

    Models a tokenizer/counter whose parts do not add up, so the feedback that
    ``_fit_part`` sized still overflows and the last-resort fallback runs.
    """

    source = "tokenize"

    def __init__(self, server: _Server) -> None:
        self.server = server

    async def __call__(self, text: str) -> int:
        tokens = await self.server.count_tokens(text)
        return tokens + (100_000 if "Correction (round" in text else 0)


class HandoverPrefixTests(unittest.TestCase):
    """#685 review blocker: a round must never refit the handover before its feedback."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (("init", "-q"), ("config", "user.email", "t@example.com"), ("config", "user.name", "T")):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "value.txt").write_text("original\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True, capture_output=True)

    def _run(self, server: _Server, task: str, rates=(None, None), **worker_kw):
        log = _Logger()
        worker = LocalCodingWorker(client=server, max_tokens=1024, rates_path=self.root / "rates.json",
                                   **worker_kw)
        with patch.object(worker.rates, "medians", return_value=rates):
            result = worker.run(task=task, working_dir=str(self.repo), sandbox="workspace-write",
                                experiment_logger=log)
        return worker, result, log

    def _assert_rounds_share_round_1(self, server: _Server, plan) -> None:
        first = server.calls[0]["prompt"]
        shared = _shared(first)
        self.assertIn(plan.handover_kept, first)
        for later in server.calls[1:]:
            self.assertTrue(later["prompt"].startswith(shared))
            self.assertTrue(later["prompt"][len(shared):].startswith("Correction (round "))

    def test_handover_cut_by_n_ctx_is_resent_verbatim_in_rounds_2_and_3(self):
        bad, good = _answers()
        # A long error each round: the old code took the room for it out of the handover.
        huge_bad = bad + "".join(f"junk line {i} {'x' * 50}\n" for i in range(300))
        server = _Server([huge_bad, huge_bad, good], n_ctx=4096)
        worker, result, log = self._run(server, "Edit value.txt\n\n" + _handover(3000))
        self.assertTrue(result.success, result.error)
        plan = worker.last_plan
        self.assertTrue(plan.handover_cut)
        self.assertGreater(plan.handover_tokens_kept, 0)
        self.assertEqual(3, len(server.calls))
        self._assert_rounds_share_round_1(server, plan)
        rounds = log.fields("local_patch_round")
        self.assertEqual([plan.handover_tokens_kept] * 2, [r["handover_tokens_kept"] for r in rounds[1:]])
        context_budget = 4096 - 1024 - 256
        for call in server.calls:
            self.assertLessEqual(asyncio.run(server.count_tokens(_SYSTEM_PROMPT))
                                 + asyncio.run(server.count_tokens(call["prompt"])), context_budget)

    def test_handover_cut_by_the_time_budget_is_resent_verbatim(self):
        server = _Server(list(_answers()))
        worker, result, _log = self._run(server, "Edit value.txt\n\n" + _handover(800), rates=(20.0, 20.0))
        self.assertTrue(result.success, result.error)
        self.assertTrue(worker.last_plan.handover_cut)
        self.assertTrue(worker.last_plan.time_budget_limited)
        self._assert_rounds_share_round_1(server, worker.last_plan)

    def test_both_edit_formats(self):
        for fmt in ("diff", "search_replace"):
            with self.subTest(fmt=fmt), patch.dict(os.environ, {"LOCAL_EDIT_FORMAT": fmt}):
                subprocess.run(["git", "checkout", "--", "value.txt"], cwd=self.repo, check=True)
                server = _Server(list(_answers()), n_ctx=4096)
                worker, result, _log = self._run(server, "Edit value.txt\n\n" + _handover(3000))
                self.assertTrue(result.success, result.error)
                self._assert_rounds_share_round_1(server, worker.last_plan)

    def test_first_fit_reserves_room_for_the_feedback(self):
        server = _Server([], n_ctx=4096)
        plan = asyncio.run(build_prompt("Edit value.txt\n\n" + _handover(3000), str(self.repo), server,
                                        max_output_tokens=1024, admission=False))
        self.assertTrue(plan.handover_cut)
        self.assertLessEqual(plan.prompt_tokens, plan.budget_tokens - FEEDBACK_RESERVE_TOKENS)
        # A handover that fits with the reserve to spare is not cut at all.
        small = asyncio.run(build_prompt("Edit value.txt\n\n" + _handover(5), str(self.repo), server,
                                         max_output_tokens=1024, admission=False))
        self.assertFalse(small.handover_cut)
        self.assertEqual(small.handover, small.handover_kept)

    def test_fallback_shrinks_feedback_never_the_handover(self):
        server = _Server([], n_ctx=4096)
        plan = asyncio.run(build_prompt("Edit value.txt\n\n" + _handover(3000), str(self.repo), server,
                                        max_output_tokens=1024, admission=False))
        plan.counter = _NonAdditiveCounter(server)
        fb = asyncio.run(build_feedback_prompt(plan, server, round_no=2, error="does not apply",
                                               previous_diff="a previous answer\n"))
        # Nothing of the feedback fits, so round 2 is round 1 again - never a
        # prompt with a shorter handover.
        self.assertEqual(plan.prompt, fb.prompt)
        self.assertEqual(0, fb.error_chars)
        self.assertEqual(plan.handover_tokens_kept, fb.handover_tokens_kept)

    def test_feedback_prompt_is_deterministic_when_built_twice(self):
        server = _Server([], n_ctx=4096)
        plan = asyncio.run(build_prompt("Edit value.txt\n\n" + _handover(3000), str(self.repo), server,
                                        max_output_tokens=1024, admission=False))
        kw = dict(round_no=2, error="does not apply", previous_diff="x\n" * 2000)
        first = asyncio.run(build_feedback_prompt(plan, server, **kw))
        again = asyncio.run(build_feedback_prompt(plan, server, **kw))
        self.assertEqual(first, again)
        # Building it must not mutate the plan a later round depends on.
        self.assertEqual(plan.prompt, asyncio.run(build_prompt(
            "Edit value.txt\n\n" + _handover(3000), str(self.repo), server,
            max_output_tokens=1024, admission=False)).prompt)

    def test_subtask_context_is_resent_verbatim(self):
        server = _Server([], n_ctx=4096)
        worker = LocalCodingWorker(client=server, max_tokens=1024)
        parent = asyncio.run(build_prompt("Edit value.txt", str(self.repo), server,
                                          max_output_tokens=1024, admission=False))
        earlier = ["".join(f"+earlier change {i} {'y' * 40}\n" for i in range(800))]
        sub = asyncio.run(worker._subtask_plan(parent, Subtask("edit it", ("value.txt",)), 2, 2,
                                               earlier, str(self.repo)))
        self.assertTrue(sub.handover_cut)
        fb = asyncio.run(build_feedback_prompt(sub, server, round_no=2, error="does not apply\n" * 80,
                                               previous_diff="z\n" * 3000))
        self.assertTrue(fb.prompt.startswith(_shared(sub.prompt)))
        self.assertGreater(fb.error_chars, 0)


class ResilienceTests(unittest.TestCase):
    """Interrupted, crashed, half-measured and repeated steps never over-credit the cache."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (("init", "-q"), ("config", "user.email", "t@example.com"), ("config", "user.name", "T")):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "value.txt").write_text("original\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True, capture_output=True)

    def _run(self, worker: LocalCodingWorker, rates=(None, None)):
        log = _Logger()
        subprocess.run(["git", "checkout", "--", "value.txt"], cwd=self.repo, check=True)
        with patch.object(worker.rates, "medians", return_value=rates):
            result = worker.run(task="Edit value.txt", working_dir=str(self.repo),
                                sandbox="workspace-write", experiment_logger=log)
        return result, log

    def test_failed_call_is_never_credited_to_the_next_one(self):
        class Crashing(_Server):
            async def complete(self, prompt, **kw):
                await super().complete(prompt, **kw)
                raise RuntimeError("server crashed mid-generation")

        server = Crashing([])
        worker = LocalCodingWorker(client=server, max_tokens=1024, rates_path=self.root / "rates.json")
        result, _log = self._run(worker)
        self.assertFalse(result.success)
        self.assertIsNotNone(worker._last_sent)
        self.assertFalse(worker._last_sent.completed)
        self.assertEqual(0, asyncio.run(worker._prefix_reuse(_SYSTEM_PROMPT, server.calls[0]["prompt"])))

    def test_timeout_mid_call_records_no_reuse_and_next_run_starts_cold(self):
        import httpx

        class TimesOut(_Server):
            fail = True

            async def complete(self, prompt, **kw):
                text = await super().complete(prompt, **kw)
                if self.fail:
                    self.last_timings = None  # interrupted: the server reported nothing
                    raise httpx.ReadTimeout("timed out")
                return text

        server = TimesOut(list(_answers()))
        worker = LocalCodingWorker(client=server, max_tokens=1024, rates_path=self.root / "rates.json")
        result, log = self._run(worker)
        self.assertFalse(result.success)
        self.assertFalse(worker._last_sent.completed)
        server.fail = False
        server.calls.clear()
        server.responses = list(_answers())
        result, log = self._run(worker)
        self.assertTrue(result.success, result.error)
        calls = log.fields("local_model_call")
        self.assertEqual(0, calls[0]["prefix_reuse_tokens"])  # nothing credited after the timeout
        self.assertGreater(calls[1]["prefix_reuse_tokens"], 0)

    def test_tokenize_crash_halfway_falls_back_without_failing_the_run(self):
        class FlakyTokenize(_Server):
            calls_to_tokenize = 0

            async def tokenize(self, text):
                FlakyTokenize.calls_to_tokenize += 1
                if FlakyTokenize.calls_to_tokenize > 1:  # the second of the pair fails
                    raise RuntimeError("/tokenize went away")
                return await super().tokenize(text)

            async def count_tokens(self, text):
                return len(await _Server.tokenize(self, text))

        server = FlakyTokenize(list(_answers()))
        worker = LocalCodingWorker(client=server, max_tokens=1024, rates_path=self.root / "rates.json")
        result, log = self._run(worker)
        self.assertTrue(result.success, result.error)
        calls = log.fields("local_model_call")
        # The estimate under-counts, it never claims more than the real prefix.
        real = asyncio.run(_Server([]).count_tokens(_shared(server.calls[0]["prompt"])))
        self.assertLessEqual(calls[1]["prefix_reuse_tokens"], real + 1)
        self.assertGreaterEqual(calls[1]["prefix_reuse_tokens"], 0)

    def test_measuring_errors_give_zero_not_a_failure(self):
        worker = LocalCodingWorker(client=_Server([]))
        worker._last_sent = SentPrompt(0, _SYSTEM_PROMPT, "abc\n" * 100, 5, True)

        async def broken(_text):
            raise RuntimeError("counter crashed")

        with patch("modules.local_coding_worker.common_prefix_tokens", side_effect=RuntimeError("boom")):
            self.assertEqual(0, asyncio.run(worker._prefix_reuse(_SYSTEM_PROMPT, "abc\n" * 100)))
        self.assertEqual(0, asyncio.run(common_prefix_tokens(object(), broken, "", "x")))

    def test_running_twice_starts_cold_and_does_not_accumulate(self):
        server = _Server(list(_answers()))
        worker = LocalCodingWorker(client=server, max_tokens=1024, rates_path=self.root / "rates.json")
        _result, first_log = self._run(worker)
        server.calls.clear()
        server.responses = list(_answers())
        _result, second_log = self._run(worker)
        first, = first_log.fields("local_run_metrics")
        second, = second_log.fields("local_run_metrics")
        # Another process may have used the slot between runs: no credit for the first call.
        self.assertEqual(0, second_log.fields("local_model_call")[0]["prefix_reuse_tokens"])
        self.assertEqual(first["prefix_reuse_tokens"], second["prefix_reuse_tokens"])
        self.assertEqual(first["prefix_reuse_ratio"], second["prefix_reuse_ratio"])

    def test_schema_migration_runs_twice(self):
        db = str(self.root / "agent.db")
        init_db(db).close()
        init_db(db).close()
        conn = sqlite3.connect(db)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(local_replays)")]
        conn.close()
        self.assertEqual(1, columns.count("prefix_reuse_tokens"))
        self.assertEqual(1, columns.count("prefix_reuse_ratio"))


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
