"""Failure handling and stale-failure assessment of :class:`ExperimentAdoptionService`."""

from __future__ import annotations

import json
import logging

from typing import Any

from modules.experiment_service import (
    _truncate_text,
    _utcnow_iso,
)
from modules.github_work_tracking import LIFECYCLE_READINESS_MESSAGE
from modules.redaction import sanitized_reason

from modules.experiment_adoption_parts.core import (
    ExperimentAdoption,
    REVIEW_BLOCKER_GITHUB_LIFECYCLE,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_NEEDS_REVIEW,
    StaleFailureAssessment,
    _MAX_STALE_RETRIES,
    _safe_list,
)

logger = logging.getLogger("modules.experiment_adoption_service")

class FailureMixin:
    """Failure handling and stale-failure assessment of :class:`ExperimentAdoptionService`."""

    def _report_issue_progress(self, experiment_id: str, state: str, detail: str | None = None) -> None:
        """Best-effort Issue progress reporting for adoption outcomes."""
        post = getattr(self.github_work_tracking, "post_progress", None)
        if post is None:
            return
        try:
            post(experiment_id, state, detail=detail)
        except Exception:  # noqa: BLE001 - lifecycle reporting is informational
            return

    def assess_failure(self, experiment_id: str) -> StaleFailureAssessment:
        """Decide whether a persisted adoption failure still stands.

        ``needs_review`` rows are assessed too (#171, :meth:`_assess_needs_review`):
        retryable only when the row names a re-evaluable blocker that no longer
        holds. For ``failed``/``interrupted`` the rule is narrow and evidence-based,
        never a keyword match: a non-zero ``test_exit_code`` is a verdict about the
        change and stays terminal; an unset one means no verdict was reached, and
        such a failure is superseded only by green authoritative CI for the exact
        verified checkpoint now (#136, #134). A genuinely broken implementation is
        never retried - its CI is red or missing. The outcome is logged once here as
        ``stale_adoption_assessment`` (#141).
        """
        elog = self._elog(experiment_id)
        assessment = self._assess_failure(experiment_id, elog)
        level = "info" if assessment.retryable else "warning"
        getattr(elog, level)(
            "assessment",
            "stale_adoption_assessment",
            "Assessed whether the recorded adoption failure still stands",
            retryable=assessment.retryable,
            category=assessment.category,
            detail=assessment.detail,
            previous_reason=assessment.previous_reason,
            evidence_commit_sha=assessment.evidence_commit_sha,
            evidence_run_id=assessment.evidence_run_id,
            stale_retry_attempts=self.get(experiment_id).stale_retry_attempts,
            max_stale_retries=_MAX_STALE_RETRIES,
        )
        return assessment


    def _assess_failure(self, experiment_id: str, elog: Any) -> StaleFailureAssessment:
        """The decision itself; :meth:`assess_failure` logs its outcome."""
        record = self.get(experiment_id)
        if record.status not in {STATUS_FAILED, STATUS_INTERRUPTED, STATUS_NEEDS_REVIEW}:
            return StaleFailureAssessment(
                False, "not_failed", f"adoption status is {record.status}"
            )

        previous_reason = record.reason
        if record.test_exit_code is not None and record.test_exit_code != 0:
            return StaleFailureAssessment(
                False,
                "test_verdict",
                "the recorded failure is a test verdict about the change "
                f"(exit code {record.test_exit_code}) and stays terminal",
                previous_reason=previous_reason,
            )

        if record.stale_retry_attempts >= _MAX_STALE_RETRIES:
            return StaleFailureAssessment(
                False,
                "budget_exhausted",
                f"the adoption was already retried {record.stale_retry_attempts} time(s) "
                "on fresh evidence and kept failing; manual review required",
                previous_reason=previous_reason,
            )

        if record.status == STATUS_NEEDS_REVIEW:
            return self._assess_needs_review(record)

        try:
            evidence = self._exact_commit_ci(
                experiment_id, phase="assessment", elog=elog
            )
        except Exception as exc:  # noqa: BLE001 - missing evidence must not raise into the caller
            logger.warning(
                "could not read authoritative CI evidence for %s", experiment_id, exc_info=True
            )
            elog.error(
                "assessment",
                "ci_evidence_lookup_failed",
                "Authoritative CI evidence could not be read at all",
                exc=exc,
            )
            return StaleFailureAssessment(
                False,
                "evidence_unavailable",
                "authoritative CI evidence could not be read",
                previous_reason=previous_reason,
            )

        ci = evidence.verification
        if not evidence.passed:
            # Why the evidence does not support a retry was already named by
            # `_exact_commit_ci`; repeating it here would log one decision twice.
            return StaleFailureAssessment(
                False,
                "no_ci_evidence",
                "no successful authoritative CI for the exact verified checkpoint "
                f"(status={ci.status}; reason={ci.reason or 'none'})",
                previous_reason=previous_reason,
            )

        return StaleFailureAssessment(
            True,
            "superseded_local_verification",
            "the recorded failure never produced a test verdict and authoritative "
            f"GitHub Actions passed for the exact checkpoint {evidence.checkpoint_sha}"
            f" (run_id={ci.run_id})",
            previous_reason=previous_reason,
            evidence_commit_sha=evidence.checkpoint_sha,
            evidence_run_id=ci.run_id,
        )


    def _assess_needs_review(self, record: ExperimentAdoption) -> StaleFailureAssessment:
        """Decide whether a persisted ``needs_review`` may be resumed (#171).

        Some needs_review rows record an external condition (e.g. the GitHub
        lifecycle was unconfigured, #115/#116); those are re-checkable, and the
        re-check asks the *live* dependency, never the recorded text, and only
        for a blocker the row actually names. Anything else (file mismatch,
        denied path, failed merge, manual review) stays terminal.
        """
        blocker = self._review_blocker_of(record)
        previous_reason = record.reason
        if blocker is None:
            return StaleFailureAssessment(
                False,
                "manual_review",
                "the recorded needs_review is not tied to a re-evaluable "
                "condition and stays terminal",
                previous_reason=previous_reason,
            )

        if blocker == REVIEW_BLOCKER_GITHUB_LIFECYCLE:
            try:
                self.github_work_tracking.require_lifecycle_ready()
            except Exception as exc:  # noqa: BLE001 - a blocker that still holds is not an error
                return StaleFailureAssessment(
                    False,
                    "review_blocker_persists",
                    f"the recorded needs_review blocker {blocker} still applies: {exc}",
                    previous_reason=previous_reason,
                )
            return StaleFailureAssessment(
                True,
                "review_blocker_cleared",
                f"the recorded needs_review blocker {blocker} no longer applies; "
                "the GitHub lifecycle is ready now",
                previous_reason=previous_reason,
            )

        return StaleFailureAssessment(
            False,
            "manual_review",
            f"the recorded needs_review blocker {blocker} cannot be re-evaluated",
            previous_reason=previous_reason,
        )


    @staticmethod
    def _review_blocker_of(record: ExperimentAdoption) -> str | None:
        """The row's named blocker, recognizing rows written before #171 via the
        producer's own exported marker (``LIFECYCLE_READINESS_MESSAGE``) rather
        than guessed keywords."""
        blocker = (record.review_blocker or "").strip()
        if blocker:
            return blocker
        if LIFECYCLE_READINESS_MESSAGE in (record.reason or ""):
            return REVIEW_BLOCKER_GITHUB_LIFECYCLE
        return None


    def _finish_failure(
        self,
        experiment_id: str,
        *,
        reason: str,
        actual_files: list[str] | None = None,
        expected_files: list[str] | None = None,
        test_exit_code: int | None = None,
        test_output: str | None = None,
        experiment_commit_sha: str | None = None,
        integrated_commit_sha: str | None = None,
        diagnostic_worktree_path: str | None = None,
        elog=None,
    ) -> ExperimentAdoption:
        if elog is not None:
            elog.warning("adoption", "adoption_failed", reason)
        self._set_status(
            experiment_id,
            STATUS_FAILED,
            ended_at=_utcnow_iso(),
            experiment_commit_sha=experiment_commit_sha,
            integrated_commit_sha=integrated_commit_sha,
            expected_files_json=json.dumps(_safe_list(expected_files)),
            actual_files_json=json.dumps(_safe_list(actual_files)),
            test_exit_code=test_exit_code,
            test_output=_truncate_text(test_output, 32 * 1024, label="test output"),
            reason=sanitized_reason(reason),
            diagnostic_worktree_path=diagnostic_worktree_path,
            updated_at=_utcnow_iso(),
        )
        self._report_issue_progress(
            experiment_id, "failed", f"Reason: {sanitized_reason(reason)}"
        )
        return self.get(experiment_id)


    def _finish_review(
        self,
        experiment_id: str,
        *,
        reason: str,
        actual_files: list[str] | None = None,
        expected_files: list[str] | None = None,
        test_exit_code: int | None = None,
        test_output: str | None = None,
        experiment_commit_sha: str | None = None,
        integrated_commit_sha: str | None = None,
        diagnostic_worktree_path: str | None = None,
        review_blocker: str | None = None,
        status: str = STATUS_NEEDS_REVIEW,
        elog=None,
    ) -> ExperimentAdoption:
        """Park the adoption for review. ``review_blocker`` names a re-evaluable
        external condition when there is one (#171) - deliberately not derived
        from ``reason``, which is the human explanation; ``None`` keeps the
        review terminal, the default for judgements about the change itself."""
        if elog is not None:
            elog.warning(
                "adoption",
                "adoption_needs_review",
                reason,
                review_blocker=review_blocker,
            )
        self._set_status(
            experiment_id,
            status,
            review_blocker=review_blocker,
            ended_at=_utcnow_iso(),
            experiment_commit_sha=experiment_commit_sha,
            integrated_commit_sha=integrated_commit_sha,
            expected_files_json=json.dumps(_safe_list(expected_files)),
            actual_files_json=json.dumps(_safe_list(actual_files)),
            test_exit_code=test_exit_code,
            test_output=_truncate_text(test_output, 32 * 1024, label="test output"),
            reason=sanitized_reason(reason),
            diagnostic_worktree_path=diagnostic_worktree_path,
            updated_at=_utcnow_iso(),
        )
        self._report_issue_progress(
            experiment_id, "needs_review", f"Reason: {sanitized_reason(reason)}"
        )
        return self.get(experiment_id)
