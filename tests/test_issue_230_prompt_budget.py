"""#230: the controller prompt is bounded by the context window, as rendered.

The old budget split a number five ways and compacted two of the parts; the
rules and the capability catalog were emitted whole on top, so a 500-char
"budget" sat next to a 9946-char prompt. These tests render the real prompt
through ``_build_prompt`` and hold it to the budget it claims.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.nexus_task_store import NexusTaskStore
from modules.prompt_budget import PromptBudgetExhaustedError, compute_budget
from modules.task_orchestrator import NexusTaskOrchestrator

OBSERVATION_MARKER = "ANSWER-TO-THE-CURRENT-TASK"
HISTORY_MARKER = "old-history-event"


def _observation(event_id: int, size: int = 3000) -> dict:
    return {
        "tool": "command",
        "event_id": event_id,
        "status": "succeeded",
        "result": f"{OBSERVATION_MARKER}-{event_id} " + "o" * size,
    }


def _history(count: int, size: int = 1500) -> list[dict]:
    return [
        {
            "role": "assistant",
            "kind": "decision",
            "content": f"{HISTORY_MARKER}-{i} " + "h" * size,
            "metadata": {},
            "created_at": "2026-11-06T00:00:00Z",
        }
        for i in range(count)
    ]


class RenderedPromptBudgetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.orchestrator = NexusTaskOrchestrator(
            operations_registry=SimpleNamespace(
                read_only_specs=lambda: [], catalog=lambda: [], repo_root=self._tmp.name
            ),
            experiment_service=SimpleNamespace(resolve_experiment=lambda _id: None),
            task_store=NexusTaskStore(db_path=str(Path(self._tmp.name) / "nexus.db")),
        )
        self.session = SimpleNamespace(id="s-230", source_type="task", source_id="230", title="t")

    def tearDown(self):
        self.orchestrator.close()
        self._tmp.cleanup()

    def _plan(self, **kwargs):
        return compute_budget(
            fixed_chars=self.orchestrator._controller_fixed_chars(self.session),
            mem_available_kb=None,
            **kwargs,
        )

    def test_fixed_cost_is_the_measured_length_of_the_rendered_fixed_sections(self):
        fixed = self.orchestrator._controller_fixed_chars(self.session)
        empty_prompt = self.orchestrator._prompt(self.session, "", [], [])
        self.assertEqual(fixed, len(empty_prompt))
        self.assertIn("RULES:", empty_prompt)
        self.assertIn("AVAILABLE CAPABILITIES:", empty_prompt)

    def test_the_full_prompt_fits_its_stated_budget(self):
        for context_tokens in (8192, 4096, 3600):
            with self.subTest(model_context_tokens=context_tokens):
                plan = self._plan(model_context_tokens=context_tokens)
                prompt, meta = self.orchestrator._build_prompt(
                    self.session,
                    "solve issue #230 " + "t" * 20000,
                    _history(18),
                    [_observation(i, 8000) for i in range(6)],
                    plan,
                )
                self.assertLessEqual(len(prompt), plan.effective_budget_chars)
                self.assertTrue(meta["input_truncated"])
                # The fixed sections are still all there.
                self.assertIn("RULES:", prompt)
                self.assertIn("AVAILABLE CAPABILITIES:", prompt)

    def test_observations_survive_ahead_of_history_when_room_is_short(self):
        plan = self._plan(model_context_tokens=3600)
        history = _history(12)
        prompt, meta = self.orchestrator._build_prompt(
            self.session, "solve issue #230", history, [_observation(1, 2500)], plan
        )
        self.assertLessEqual(len(prompt), plan.effective_budget_chars)
        # The result the next decision depends on is there in full...
        self.assertIn(f"{OBSERVATION_MARKER}-1", prompt)
        self.assertIn("o" * 2500, prompt)
        self.assertEqual(meta["omitted_observation_indices"], [])
        # ...and the re-readable history gave way, oldest first.
        self.assertGreater(meta["history_events_dropped"], 0)
        self.assertNotIn(f"{HISTORY_MARKER}-0 ", prompt)

    def test_history_uses_the_room_observations_leave(self):
        plan = self._plan()
        prompt, meta = self.orchestrator._build_prompt(
            self.session, "solve issue #230", _history(4, 500), [], plan
        )
        self.assertLessEqual(len(prompt), plan.effective_budget_chars)
        self.assertEqual(meta["history_events"], 4)
        self.assertEqual(meta["history_events_dropped"], 0)

    def test_a_budget_the_fixed_sections_exhaust_fails_loudly(self):
        plan = self._plan(model_context_tokens=1200)
        self.assertTrue(plan.starved)
        with self.assertRaises(PromptBudgetExhaustedError):
            self.orchestrator._build_prompt(
                self.session, "solve issue #230", [], [_observation(1)], plan
            )

    def test_low_memory_does_not_shrink_the_rendered_prompt(self):
        roomy = self._plan()
        pressed = compute_budget(
            fixed_chars=self.orchestrator._controller_fixed_chars(self.session),
            mem_available_kb=1024,
        )
        self.assertTrue(pressed.low_memory)
        args = (self.session, "solve issue #230", _history(3, 300), [_observation(1)])
        roomy_prompt, _ = self.orchestrator._build_prompt(*args, roomy)
        pressed_prompt, _ = self.orchestrator._build_prompt(*args, pressed)
        self.assertEqual(pressed_prompt, roomy_prompt)
        self.assertIn(f"{OBSERVATION_MARKER}-1", pressed_prompt)


if __name__ == "__main__":
    unittest.main()
