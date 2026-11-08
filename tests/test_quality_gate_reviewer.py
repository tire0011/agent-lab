"""Quality-gate artifact findings go to the reviewer instead of stopping (#519)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.experiment_adoption_service import ExperimentAdoptionService
from modules.experiment_evaluation import EvaluationCheck, EvaluationResult
from modules.nexus_reviewer import ReviewResult, review_prompt

FINDINGS = "erforderliches Artefakt modules/x.py fehlt"


def _result(*checks: EvaluationCheck, changed=("modules/a.py",)) -> EvaluationResult:
    return EvaluationResult("needs_review", FINDINGS, list(checks), changed_files=list(changed))


class SoftFindingsTests(unittest.TestCase):
    def test_only_artifact_checks_failed_is_soft(self):
        result = _result(
            EvaluationCheck("non_test_evidence", True, "ok"),
            EvaluationCheck("required_artifacts_present", False, "missing modules/x.py"),
        )
        self.assertEqual(result.soft_findings(), FINDINGS)

    def test_scope_failure_stays_hard(self):
        result = _result(
            EvaluationCheck("required_artifacts_present", False, "missing"),
            EvaluationCheck("tests_only_scope", False, "non-test file changed"),
        )
        self.assertIsNone(result.soft_findings())

    def test_empty_diff_stays_hard(self):
        result = _result(EvaluationCheck("non_test_evidence", False, "nothing"), changed=())
        self.assertIsNone(result.soft_findings())

    def test_pass_has_no_findings(self):
        self.assertIsNone(EvaluationResult("pass", "", []).soft_findings())


class ReviewPromptTests(unittest.TestCase):
    def test_findings_are_named_in_the_prompt(self):
        prompt = review_prompt("task", "diff", can_read_files=True, gate_findings=FINDINGS)
        self.assertIn("QUALITY GATE FINDINGS", prompt)
        self.assertIn(FINDINGS, prompt)

    def test_no_findings_no_section(self):
        self.assertNotIn("QUALITY GATE", review_prompt("task", "diff", can_read_files=True))


def _service(reviewer) -> ExperimentAdoptionService:
    service = ExperimentAdoptionService.__new__(ExperimentAdoptionService)
    service.reviewer = reviewer
    service.github_work_tracking = MagicMock()
    service._integration_base_sha = lambda: "base"
    return service


def _blocks(service, findings=FINDINGS):
    return service._review_blocks(
        "exp", "goal", SimpleNamespace(pr_number=1), "/tmp/x", None, gate_findings=findings
    )


class ReviewDecidesTests(unittest.TestCase):
    def _reviewer(self, verdict=None, exc=None):
        reviewer = MagicMock()
        if exc is not None:
            reviewer.review.side_effect = exc
        else:
            reviewer.review.return_value = ReviewResult("claude", verdict, "summary")
        return reviewer

    def test_approve_merges_and_reviewer_got_the_findings(self):
        reviewer = self._reviewer("approve")
        self.assertIsNone(_blocks(_service(reviewer)))
        self.assertEqual(reviewer.review.call_args.kwargs["gate_findings"], FINDINGS)

    def test_block_goes_back_through_the_review_retry(self):
        reason = _blocks(_service(self._reviewer("block")))
        self.assertTrue(reason.startswith("Review by claude blocked the merge"))

    def test_no_verdict_does_not_merge(self):
        self.assertIn(FINDINGS, _blocks(_service(self._reviewer("unreviewed"))))
        self.assertIn(FINDINGS, _blocks(_service(self._reviewer(exc=RuntimeError("down")))))
        self.assertIn(FINDINGS, _blocks(_service(None)))

    def test_without_findings_behaviour_is_unchanged(self):
        reviewer = self._reviewer(exc=RuntimeError("down"))
        self.assertIsNone(_blocks(_service(reviewer), findings=""))
        self.assertNotIn("gate_findings", reviewer.review.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
