from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from modules.prompt_budget import (
    compact_observations,
    compute_budget,
    dedupe_nested_results,
    read_mem_available_kb,
    read_swap_free_kb,
    truncate_text,
)


class ComputeBudgetTests(unittest.TestCase):
    def test_effective_budget_never_exceeds_context_window(self):
        plan = compute_budget(model_context_tokens=2048, reserved_output_tokens=500, mem_available_kb=None)
        self.assertLessEqual(plan.effective_budget_chars, plan.context_budget_chars)
        self.assertEqual(plan.context_budget_chars, (2048 - 500) * 4)

    def test_low_memory_shrinks_effective_budget_below_context_window(self):
        generous = compute_budget(
            model_context_tokens=8192,
            reserved_output_tokens=700,
            mem_available_kb=8_000_000,
            mem_limit_mb=2048,
        )
        starved = compute_budget(
            model_context_tokens=8192,
            reserved_output_tokens=700,
            mem_available_kb=1024,  # ~1MB free, far below the 2048MB limit
            mem_limit_mb=2048,
        )
        self.assertFalse(generous.low_memory)
        self.assertTrue(starved.low_memory)
        self.assertLess(starved.effective_budget_chars, generous.effective_budget_chars)
        self.assertLessEqual(starved.safe_local_budget_chars, starved.effective_budget_chars)
        # The low-memory branch must actually shrink the safe local budget,
        # not just re-apply the value effective_budget_chars already used.
        self.assertEqual(starved.safe_local_budget_chars, 0)

    def test_realistic_low_memory_range_blocks_local_calls(self):
        # A host with only 100-500MB free is already dangerously low when
        # the configured local model needs ~2048MB just to run; the old
        # heuristic only tripped below ~15MB and let these values through
        # with the full context budget still allowed.
        for mem_available_kb in (100_000, 250_000, 500_000):
            with self.subTest(mem_available_kb=mem_available_kb):
                plan = compute_budget(
                    model_context_tokens=8192,
                    reserved_output_tokens=700,
                    mem_available_kb=mem_available_kb,
                    mem_limit_mb=2048,
                )
                self.assertTrue(plan.low_memory)
                self.assertEqual(plan.safe_local_budget_chars, 0)

    def test_low_swap_forces_low_memory_even_with_high_mem_available(self):
        plan = compute_budget(
            model_context_tokens=8192,
            reserved_output_tokens=700,
            mem_available_kb=8_000_000,
            swap_free_kb=1024,
            mem_limit_mb=2048,
        )
        self.assertTrue(plan.low_memory)
        self.assertEqual(plan.safe_local_budget_chars, 0)

    def test_sections_sum_to_effective_budget(self):
        plan = compute_budget(model_context_tokens=8192, reserved_output_tokens=700, mem_available_kb=None)
        total = (
            plan.instructions_chars
            + plan.tool_schema_chars
            + plan.current_input_chars
            + plan.history_chars
            + plan.observation_chars
        )
        self.assertEqual(total, plan.effective_budget_chars)

    def test_budget_is_configurable_via_explicit_arguments(self):
        small = compute_budget(model_context_tokens=1024, reserved_output_tokens=200, mem_available_kb=None)
        large = compute_budget(model_context_tokens=32768, reserved_output_tokens=200, mem_available_kb=None)
        self.assertLess(small.effective_budget_chars, large.effective_budget_chars)


class ReadMemAvailableTests(unittest.TestCase):
    def test_reads_kb_value_from_meminfo_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meminfo"
            path.write_text("MemTotal:       16000000 kB\nMemAvailable:    4096 kB\nSwapTotal:       0 kB\n")
            self.assertEqual(read_mem_available_kb(str(path)), 4096)

    def test_missing_file_returns_none(self):
        self.assertIsNone(read_mem_available_kb("/nonexistent/meminfo"))


class ReadSwapFreeTests(unittest.TestCase):
    def test_reads_kb_value_from_meminfo_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meminfo"
            path.write_text("MemAvailable:    4096 kB\nSwapTotal:  1000000 kB\nSwapFree:  250000 kB\n")
            self.assertEqual(read_swap_free_kb(str(path)), 250000)

    def test_missing_file_returns_none(self):
        self.assertIsNone(read_swap_free_kb("/nonexistent/meminfo"))


class TruncateTextTests(unittest.TestCase):
    def test_short_text_is_untouched(self):
        text, truncated = truncate_text("short", 100)
        self.assertEqual(text, "short")
        self.assertFalse(truncated)

    def test_long_text_is_deterministically_truncated(self):
        text, truncated = truncate_text("x" * 1000, 50)
        self.assertTrue(truncated)
        self.assertLessEqual(len(text), 50)
        self.assertIn("truncated", text)

    def test_zero_budget_omits_everything(self):
        text, truncated = truncate_text("some content", 0)
        self.assertEqual(text, "")
        self.assertTrue(truncated)


class CompactObservationsTests(unittest.TestCase):
    def test_small_observations_fit_within_generous_budget(self):
        observations = [{"tool": "operation", "result": "ok"} for _ in range(3)]
        compacted, omitted = compact_observations(observations, budget_chars=10000)
        self.assertEqual(len(compacted), 3)
        self.assertEqual(omitted, [])

    def test_oversized_history_is_bounded_and_older_entries_are_stubbed(self):
        observations = [
            {"tool": "command", "event_id": i, "result": "y" * 5000} for i in range(10)
        ]
        compacted, omitted = compact_observations(observations, budget_chars=4000, keep_recent_full=2)
        total_chars = sum(len(str(item)) for item in compacted)
        self.assertLess(total_chars, 4000 + 2000)  # generous slack for stub overhead
        self.assertTrue(omitted)
        # Older entries are stubbed with a stable event_id reference, never silently dropped.
        for index in omitted:
            self.assertTrue(compacted[index].get("truncated"))
            self.assertEqual(compacted[index].get("event_id"), observations[index]["event_id"])

    def test_recent_observations_are_prioritised_over_older_ones(self):
        observations = [
            {"tool": "command", "event_id": 1, "result": "old" * 2000},
            {"tool": "command", "event_id": 2, "result": "new" * 2000},
        ]
        compacted, omitted = compact_observations(observations, budget_chars=2000, keep_recent_full=1)
        self.assertIn(0, omitted)
        self.assertNotIn(1, omitted)


class DedupeNestedResultsTests(unittest.TestCase):
    def test_memory_search_hits_matching_already_seen_content_are_dropped(self):
        observations = [
            {
                "tool": "memory_search",
                "query": "prior work",
                "results": [
                    {"source": "task_history", "content": "Duplicate of earlier tool result"},
                    {"source": "project_document", "content": "Fresh, unseen content"},
                ],
            }
        ]
        deduped = dedupe_nested_results(
            observations, already_seen={"Duplicate of earlier tool result"}
        )
        self.assertEqual(len(deduped[0]["results"]), 1)
        self.assertEqual(deduped[0]["results"][0]["content"], "Fresh, unseen content")
        self.assertEqual(deduped[0]["deduped"], 1)

    def test_non_memory_search_observations_are_left_untouched(self):
        observations = [{"tool": "command", "result": "ok"}]
        deduped = dedupe_nested_results(observations, already_seen={"ok"})
        self.assertEqual(deduped, observations)

    def test_repeated_identical_results_across_observations_are_deduped_once(self):
        observations = [
            {"tool": "memory_search", "results": [{"content": "same finding"}]},
            {"tool": "memory_search", "results": [{"content": "same finding"}]},
        ]
        deduped = dedupe_nested_results(observations)
        self.assertEqual(len(deduped[0]["results"]), 1)
        self.assertEqual(len(deduped[1]["results"]), 0)


if __name__ == "__main__":
    unittest.main()
