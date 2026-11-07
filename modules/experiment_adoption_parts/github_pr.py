"""GitHub pull-request flow: readiness, review and merge state of :class:`ExperimentAdoptionService`."""

from __future__ import annotations

import logging

from modules.experiment_adoption_parts.core import (
    REVIEW_BLOCKER_GITHUB_LIFECYCLE,
)

logger = logging.getLogger("modules.experiment_adoption_service")

class GitHubPrMixin:
    """GitHub pull-request flow: readiness, review and merge state of :class:`ExperimentAdoptionService`."""

    def _merge_refusal_for(self, experiment_id: str) -> str | None:
        """The no-merge constraint on an experiment's existing pull request."""
        work_ref = self.github_work_tracking.for_experiment(experiment_id)
        if work_ref is None or work_ref.pr_number is None:
            return None
        return self.github_work_tracking.merge_refusal(work_ref)


    def _merged_sha_for(self, experiment_id: str) -> str | None:
        try:
            work_ref = self.github_work_tracking.for_experiment(experiment_id)
            if work_ref is None:
                return None
            return self.github_work_tracking.merged_commit_sha(work_ref)
        except Exception:
            logger.warning(
                "could not determine merge state for %s during recovery", experiment_id
            )
            return None


    def _lifecycle_readiness_blocker(self) -> str | None:
        """``REVIEW_BLOCKER_GITHUB_LIFECYCLE`` iff the lifecycle is unconfigured now.

        A ``GitHubWorkTrackingError`` is not automatically a readiness
        problem - the same call can fail because the GitHub API rejected a
        request - and only the readiness case is re-evaluable. So the
        blocker is recorded from the service's own current verdict rather
        than from the exception text.
        """
        try:
            return (
                REVIEW_BLOCKER_GITHUB_LIFECYCLE
                if self.github_work_tracking.readiness_error()
                else None
            )
        except Exception:  # noqa: BLE001 - classification must never fail the review
            logger.warning("could not classify GitHub lifecycle readiness", exc_info=True)
            return None


    def _review_blocks(
        self, experiment_id, goal, work_ref, integration_path, elog, *, gate_findings: str = "",
    ) -> str | None:
        """Review the change before the merge (#396); the reason if it is blocked.

        The review result goes on the pull request either way. A review that
        cannot run does not block: tests and the quality gate already passed.
        With quality-gate findings (#519) only a real verdict may merge: the
        reviewer was asked to judge them, so no verdict means no merge.
        """
        no_verdict = (
            f"Qualitäts-Gate-Befund ohne Reviewer-Urteil: {gate_findings}" if gate_findings else None
        )
        if self.reviewer is None:
            return no_verdict
        kwargs = {"gate_findings": gate_findings} if gate_findings else {}
        try:
            result = self.reviewer.review(
                experiment_id=experiment_id,
                goal=str(goal or ""),
                worktree_path=str(integration_path),
                base_sha=self._integration_base_sha(),
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - a broken review must not block
            logger.warning("adoption review failed for %s: %s", experiment_id, exc)
            return no_verdict
        from modules.nexus_reviewer import review_comment

        if elog is not None:
            elog.info("adoption", "review_completed", f"{result.reviewer}: {result.verdict}")
        try:
            self.github_work_tracking.comment(work_ref.pr_number, review_comment(result))
        except Exception as exc:  # noqa: BLE001 - the comment is informational
            logger.warning("could not post the review on PR #%s: %s", work_ref.pr_number, exc)
        if result.blocks:
            return f"Review by {result.reviewer} blocked the merge: {result.summary}"
        if gate_findings and result.verdict != "approve":
            return no_verdict
        return None
