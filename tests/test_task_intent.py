"""Tests for modules.task_intent — mailbox task intent classification."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from modules.task_intent import (
    ACTION_NEW_EXPERIMENT,
    ACTION_RESUME_EXPERIMENT,
    classify_task_intent,
)


def _make_exp_service(known_id="bd9720b26ec74f15a2693e5641adf141"):
    es = MagicMock()

    def resolve(experiment_id):
        candidate = experiment_id.lower()
        if known_id.startswith(candidate) or candidate == known_id:
            exp = MagicMock()
            exp.id = known_id
            return exp
        raise LookupError("no such experiment")

    es.resolve_experiment.side_effect = resolve
    return es


class TestClassifyTaskIntent(unittest.TestCase):
    def test_new_experiment_when_no_resume_language(self):
        es = _make_exp_service()
        decision = classify_task_intent("Add a new feature to the parser.", es)
        self.assertEqual(decision.action, ACTION_NEW_EXPERIMENT)
        self.assertIsNone(decision.experiment_id)

    def test_regression_resume_existing_experiment_by_full_id(self):
        es = _make_exp_service()
        body = (
            "Resume and finish existing experiment bd9720b26ec74f15a2693e5641adf141.\n"
            "Do not reimplement and do not create a new experiment.\n"
            "Continue using its existing worktree and verification state."
        )
        decision = classify_task_intent(body, es)
        self.assertEqual(decision.action, ACTION_RESUME_EXPERIMENT)
        self.assertEqual(decision.experiment_id, "bd9720b26ec74f15a2693e5641adf141")

    def test_resume_language_with_short_prefix(self):
        es = _make_exp_service()
        decision = classify_task_intent("Please resume experiment bd9720b2 and finish verification.", es)
        self.assertEqual(decision.action, ACTION_RESUME_EXPERIMENT)
        self.assertEqual(decision.experiment_id, "bd9720b26ec74f15a2693e5641adf141")

    def test_resume_language_without_valid_id_falls_back_to_new(self):
        es = _make_exp_service()
        decision = classify_task_intent("Resume the previous work please.", es)
        self.assertEqual(decision.action, ACTION_NEW_EXPERIMENT)

    def test_id_mentioned_without_resume_language_is_new_experiment(self):
        es = _make_exp_service()
        decision = classify_task_intent(
            "See experiment bd9720b26ec74f15a2693e5641adf141 for context, but build something else.",
            es,
        )
        self.assertEqual(decision.action, ACTION_NEW_EXPERIMENT)

    def test_resume_language_with_unknown_id_falls_back_to_new(self):
        es = _make_exp_service()
        decision = classify_task_intent("Resume experiment deadbeefdeadbeef please.", es)
        self.assertEqual(decision.action, ACTION_NEW_EXPERIMENT)

    def test_none_experiment_service_never_resumes(self):
        decision = classify_task_intent(
            "Resume experiment bd9720b26ec74f15a2693e5641adf141 please.", None
        )
        self.assertEqual(decision.action, ACTION_NEW_EXPERIMENT)


if __name__ == "__main__":
    unittest.main()
