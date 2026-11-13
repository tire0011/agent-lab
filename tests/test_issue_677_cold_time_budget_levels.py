"""#677: a cold prompt fits file excerpts to the prefill time budget."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.local_coding_worker import (
    _FILE_LEVELS,
    LocalCodingWorker,
    TimeBudget,
    TokenCounter,
    _keywords,
    _named_files,
    _render_files,
    _render_prompt,
    _SYSTEM_PROMPT,
    build_feedback_prompt,
    build_prompt_parts,
)


class _Client:
    timeout = 300.0

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def count_tokens(self, text: str) -> int:
        # Deliberately simple and exact, so excerpt boundaries are deterministic.
        return len(text.encode("utf-8"))

    async def complete(self, prompt: str, **_kwargs) -> str:
        self.calls.append(prompt)
        return "no patch"


class ColdTimeBudgetLevelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        lines = [f"line {i}: filler = {i}" for i in range(240)]
        lines[120] = "target_name = 120"
        (self.repo / "large.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.client = _Client()
        self.essential = "Change `target_name` in large.py"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _sizes(self) -> list[int]:
        files = _named_files(self.essential, str(self.repo))
        keywords = _keywords(self.essential)
        return [
            len(_SYSTEM_PROMPT.encode())
            + len(_render_prompt(self.essential, _render_files(files, keywords, level), "").encode())
            for level in _FILE_LEVELS
        ]

    def _build(self, token_budget: int, *, cold: bool):
        allowance = TimeBudget(token_budget, 1024, 240.0)
        with patch("modules.local_coding_worker.time_budget", return_value=allowance):
            return asyncio.run(build_prompt_parts(
                self.essential, "", str(self.repo), self.client,
                max_output_tokens=1024, n_ctx=100_000, n_ctx_source="test",
                counter=TokenCounter(self.client), rates=(1.0, 1.0), admission=False,
                cold=cold,
            ))

    def test_cold_round_chooses_deeper_level_within_time_budget(self):
        sizes = self._sizes()
        budget = sizes[2]
        plan = self._build(budget, cold=True)
        self.assertEqual(2, plan.file_level)
        self.assertLessEqual(plan.prompt_tokens, budget)
        self.assertTrue(plan.cold_time_limited)
        self.assertFalse(plan.time_budget_exceeded)

    def test_warm_round_keeps_context_only_file_level(self):
        budget = self._sizes()[2]
        plan = self._build(budget, cold=False)
        self.assertEqual(0, plan.file_level)
        self.assertFalse(plan.cold_time_limited)
        self.assertTrue(plan.time_budget_exceeded)

    def test_round_two_reuses_round_one_files_text_exactly(self):
        plan = self._build(self._sizes()[2], cold=True)
        feedback = asyncio.run(build_feedback_prompt(
            plan, self.client, round_no=2, error="try again", previous_diff="",
        ))
        stable = plan.stable_prefix
        self.assertTrue(plan.prompt.startswith(stable))
        self.assertTrue(feedback.prompt.startswith(stable))
        self.assertEqual(plan.files_text, stable.split(
            "Contents of target files (relevant parts):", 1)[1].rsplit("\n\n", 1)[0])

    def test_smallest_level_over_budget_is_sent_and_marked_exceeded(self):
        smallest = self._sizes()[-1]
        allowance = TimeBudget(smallest - 1, 1024, 240.0)
        worker = LocalCodingWorker(client=self.client, max_tokens=1024, split_tasks=False)
        with patch("modules.local_coding_worker.time_budget", return_value=allowance):
            result = worker.run(task=self.essential, working_dir=str(self.repo),
                                sandbox="workspace-write")
        self.assertTrue(self.client.calls)
        self.assertFalse(result.success)
        self.assertEqual(len(_FILE_LEVELS) - 1, worker.last_plan.file_level)
        self.assertTrue(worker.last_plan.time_budget_exceeded)
        self.assertTrue(worker.last_plan.cold_time_limited)


if __name__ == "__main__":
    unittest.main()
