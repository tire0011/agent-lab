"""Tests for local-LLM mailbox workflow selection."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from modules.llama_cpp_client import LlamaCppClientError
from modules.task_intent import (
    ACTION_CLARIFICATION,
    ACTION_MISSING_CAPABILITY,
    ACTION_NEW_EXPERIMENT,
    ACTION_RESUME_EXPERIMENT,
    classify_task_intent,
)


class _Client:
    def __init__(self, response: str | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.prompts: list[str] = []

    async def complete(self, *, prompt, temperature, max_tokens):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.response or "{}"


def _make_exp_service(known_id="bd9720b26ec74f15a2693e5641adf141"):
    es = MagicMock()

    def resolve(experiment_id):
        candidate = experiment_id.lower()
        if known_id.startswith(candidate) or candidate == known_id:
            exp = MagicMock()
            exp.id = known_id
            exp.status = "verifying"
            exp.worktree_path = f"/tmp/{known_id}"
            return exp
        raise LookupError("no such experiment")

    es.resolve_experiment.side_effect = resolve
    return es


class TestClassifyTaskIntent(unittest.TestCase):
    def test_local_llm_selects_new_experiment(self):
        es = _make_exp_service()
        client = _Client('{"action":"new_experiment","experiment_id":null,"reason":"new code change","message":"","command":""}')
        decision = classify_task_intent("Add a new feature to the parser.", es, client=client)
        self.assertEqual(decision.action, ACTION_NEW_EXPERIMENT)
        self.assertEqual(decision.source, "local_llm")
        self.assertIn("BEFORE any experiment is created", client.prompts[0])

    def test_regression_local_llm_resumes_existing_experiment(self):
        es = _make_exp_service()
        client = _Client(
            '{"action":"resume_experiment","experiment_id":"bd9720b2",'
            '"reason":"reuse requested worktree","message":"","command":""}'
        )
        body = (
            "Resume and finish existing experiment bd9720b26ec74f15a2693e5641adf141.\n"
            "Do not reimplement and do not create a new experiment.\n"
            "Continue using its existing worktree and verification state."
        )
        decision = classify_task_intent(body, es, client=client)
        self.assertEqual(decision.action, ACTION_RESUME_EXPERIMENT)
        self.assertEqual(decision.experiment_id, "bd9720b26ec74f15a2693e5641adf141")
        self.assertEqual(decision.source, "local_llm")
        self.assertIn("bd9720b26ec74f15a2693e5641adf141", client.prompts[0])

    def test_local_llm_can_return_missing_capability(self):
        client = _Client(
            '{"action":"missing_capability","experiment_id":null,'
            '"reason":"no calendar tool","message":"Need a calendar tool.","command":""}'
        )
        decision = classify_task_intent("Schedule a meeting for me.", _make_exp_service(), client=client)
        self.assertEqual(decision.action, ACTION_MISSING_CAPABILITY)
        self.assertEqual(decision.message, "Need a calendar tool.")
        self.assertEqual(decision.source, "local_llm")

    def test_local_llm_can_request_material_clarification(self):
        client = _Client(
            '{"action":"clarification","experiment_id":null,'
            '"reason":"two targets","message":"Which repository should I change?","command":""}'
        )
        decision = classify_task_intent("Change the config in one of the repos.", _make_exp_service(), client=client)
        self.assertEqual(decision.action, ACTION_CLARIFICATION)
        self.assertEqual(decision.message, "Which repository should I change?")

    def test_unavailable_local_model_uses_resume_fallback(self):
        es = _make_exp_service()
        client = _Client(error=LlamaCppClientError("offline"))
        decision = classify_task_intent(
            "Please resume experiment bd9720b2 and finish verification.", es, client=client
        )
        self.assertEqual(decision.action, ACTION_RESUME_EXPERIMENT)
        self.assertEqual(decision.experiment_id, "bd9720b26ec74f15a2693e5641adf141")
        self.assertEqual(decision.source, "fallback")

    def test_unavailable_local_model_preserves_legacy_new_behavior(self):
        client = _Client(error=LlamaCppClientError("offline"))
        decision = classify_task_intent("Add a feature.", _make_exp_service(), client=client)
        self.assertEqual(decision.action, ACTION_NEW_EXPERIMENT)
        self.assertEqual(decision.source, "fallback")


if __name__ == "__main__":
    unittest.main()
