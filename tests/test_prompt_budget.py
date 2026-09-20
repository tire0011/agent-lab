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
    read_swap_total_kb,
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

    def test_realistic_low_memory_range_is_flagged_but_never_blocks_local_calls(self):
        # The name matters: nothing here blocks anything. `low_memory` and
        # `safe_local_budget_chars` are advisory - the controller still calls
        # llama-server, and an external provider is used only after a real
        # local failure (see test_task_orchestrator's
        # test_low_memory_budget_is_advisory_when_local_succeeds). The model is
        # already loaded on the second call, so an attempt can well succeed
        # where a prediction said it would not.
        #
        # A host with only 100-500MB free is genuinely thin, and saying so is
        # worth doing; the old heuristic only tripped below ~15MB.
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
                # Advisory, not a gate: there is still a prompt to send.
                self.assertGreater(plan.effective_budget_chars, 0)
                self.assertGreater(plan.observation_chars, 0)

    def test_no_swap_configured_does_not_trigger_low_memory(self):
        # /proc/meminfo always reports "SwapFree: 0" when no swap device is
        # configured; that must not, by itself, make every no-swap host look
        # low on memory even with abundant MemAvailable.
        plan = compute_budget(
            model_context_tokens=8192,
            reserved_output_tokens=700,
            mem_available_kb=8_000_000,
            swap_free_kb=0,
            swap_total_kb=0,
            mem_limit_mb=2048,
        )
        self.assertFalse(plan.low_memory)
        self.assertGreater(plan.safe_local_budget_chars, 0)

    def test_low_swap_alone_does_not_trigger_low_memory_with_abundant_ram(self):
        plan = compute_budget(
            model_context_tokens=8192,
            reserved_output_tokens=700,
            mem_available_kb=8_000_000,
            swap_free_kb=1024,
            swap_total_kb=1_000_000,
            mem_limit_mb=2048,
        )
        self.assertFalse(plan.low_memory)
        self.assertGreater(plan.safe_local_budget_chars, 0)

    def test_exhausted_swap_with_thin_ram_headroom_triggers_low_memory(self):
        # Swap actually configured and nearly exhausted, combined with RAM
        # headroom barely above the cushion, is real pressure.
        #
        # "Thin" is measured against the cushion alone. It used to be measured
        # against the model's footprint plus the cushion, which asked the host
        # for room to load an already-running model a second time - so this
        # fired on hosts that were fine, and the budget collapsed to its floor.
        inference_headroom_mb = 512
        headroom_kb = inference_headroom_mb * 1024
        plan = compute_budget(
            model_context_tokens=8192,
            reserved_output_tokens=700,
            # A surplus smaller than the cushion itself: genuinely thin.
            mem_available_kb=headroom_kb + 100_000,
            swap_free_kb=1024,
            swap_total_kb=1_000_000,
            mem_limit_mb=2048,
            inference_headroom_mb=inference_headroom_mb,
        )
        self.assertTrue(plan.low_memory)
        self.assertEqual(plan.safe_local_budget_chars, 0)

    def test_a_running_model_does_not_count_against_the_prompt_budget(self):
        """The regression behind #223, in one assertion.

        These are the VM's real numbers from session
        eed2f731e16548578afca48c0dc8b47a. The model was resident in llama-server
        - already allocated, so counted in `used`, not in `MemAvailable` - and
        subtracting it again left a surplus of -24 MiB. The budget fell to its
        500-char floor, leaving 150 characters for observations, and a
        successful 3005-character github_issue result reached the controller as
        a bare stub: it could see that the call had succeeded and not what it
        returned.
        """
        plan = compute_budget(
            mem_available_kb=2_596_688,
            swap_free_kb=5_559_572,
            swap_total_kb=8_388_604,
        )

        self.assertFalse(plan.low_memory)
        # The context window is the binding limit here, as it should be.
        self.assertEqual(plan.effective_budget_chars, plan.context_budget_chars)
        # Room for a real tool result, not just a reference to one.
        self.assertGreater(plan.observation_chars, 3005)

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


class ReadSwapTotalTests(unittest.TestCase):
    def test_reads_kb_value_from_meminfo_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meminfo"
            path.write_text("MemAvailable:    4096 kB\nSwapTotal:  1000000 kB\nSwapFree:  250000 kB\n")
            self.assertEqual(read_swap_total_kb(str(path)), 1000000)

    def test_no_swap_configured_reads_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meminfo"
            path.write_text("MemAvailable:    4096 kB\nSwapTotal:       0 kB\nSwapFree:        0 kB\n")
            self.assertEqual(read_swap_total_kb(str(path)), 0)

    def test_missing_file_returns_none(self):
        self.assertIsNone(read_swap_total_kb("/nonexistent/meminfo"))


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
