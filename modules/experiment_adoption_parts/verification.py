"""Authoritative CI evidence and verification of :class:`ExperimentAdoptionService`."""

from __future__ import annotations

from typing import Any

from modules.experiment_adoption_parts.core import (
    AuthoritativeCI,
)


class VerificationMixin:
    """Authoritative CI evidence and verification of :class:`ExperimentAdoptionService`."""

    def _exact_commit_ci(
        self, experiment_id: str, *, phase: str = "adoption", elog: Any = None
    ) -> AuthoritativeCI:
        """Authoritative GitHub CI for the exact verified checkpoint (#134).

        The single definition of "authoritative", shared by the adoption
        pipeline and the stale-failure assessment: CI evidence only counts
        when it verifies the very commit adoption is about to integrate.
        ``wait_for_ci`` already refuses to report a result whose base has
        moved; the SHA comparison here rejects a result reported for some
        other commit of the same experiment.

        A verdict already accepted for that exact commit is read from the
        durable store first and is sufficient on its own (Issue #138).
        Re-deriving it needs live credentials, so a restart into a context
        without a token used to make accepted evidence vanish and left the
        #136 retry stuck at ``no_ci_evidence`` - the lifecycle is only
        restart-safe if the fact outlives the process that observed it.

        Precedence is therefore: **the most recent authoritative
        observation for the checkpoint SHA, taken on the base we are about
        to integrate onto, wins.** A live lookup runs only when no such
        verdict is stored, and whatever it finds is written through, so a
        re-run that flips the conclusion supersedes the old record. An
        implementation replaced through the normal lifecycle gets a new
        checkpoint SHA, which has no record and so goes live.

        The base is part of the question, not a detail: ``wait_for_ci``
        refuses to call CI authoritative once the target branch has moved
        past the checkpoint's parent, because cherry-picking the same
        checkpoint onto a different base produces a tree CI never saw
        (#134). Reading a stored verdict has to honour that too, so the
        base this adoption will actually integrate onto is resolved from
        the local ``origin/<target>`` ref - no credentials needed - and a
        verdict recorded against any other base is not reused.
        """
        elog = elog or self._elog(experiment_id)
        work_ref = self.github_work_tracking.for_experiment(experiment_id)
        checkpoint_sha = (work_ref.checkpoint_sha or "").strip() if work_ref is not None else ""
        integration_base = self._integration_base_sha()

        stored = (
            self.github_work_tracking.recorded_ci_details(checkpoint_sha)
            if checkpoint_sha
            else None
        )
        elog.info(
            phase,
            "ci_evidence_lookup",
            "Looking up authoritative CI evidence for the verified checkpoint",
            checkpoint_sha=checkpoint_sha or None,
            integration_base_sha=integration_base or None,
            persisted_found=stored is not None,
        )

        recorded = None
        if checkpoint_sha and integration_base:
            recorded = self.github_work_tracking.recorded_ci_for(
                checkpoint_sha, base_sha=integration_base
            )

        if recorded is not None:
            verification = recorded
            evidence_source = "persisted"
            if recorded.status == "passed":
                # `ci_evidence_reused` means an accepted, usable verdict.
                # A red one is loaded the same way but supports nothing, so
                # it is reported below as a rejection instead - one piece of
                # evidence must never be logged as both accepted and
                # rejected in the same pass.
                elog.info(
                    phase,
                    "ci_evidence_reused",
                    "Reusing persisted authoritative CI evidence; no GitHub lookup needed",
                    source="persisted",
                    checkpoint_sha=checkpoint_sha,
                    ci_status=recorded.status,
                    conclusion=recorded.conclusion,
                    run_id=recorded.run_id,
                    provider=(stored or {}).get("provider"),
                    workflow=(stored or {}).get("workflow"),
                    observed_at=(stored or {}).get("observed_at"),
                    recorded_base_sha=(stored or {}).get("base_sha"),
                    base_validated=True,
                )
        else:
            evidence_source = "live"
            # Why the store could not answer decides what the operator has
            # to fix, so name it rather than collapsing every case into
            # "no evidence".
            if stored is None:
                fallback_reason = "no_persisted_evidence"
            elif not integration_base:
                fallback_reason = "incomplete_base_evidence"
            elif not stored.get("base_sha"):
                fallback_reason = "incomplete_base_evidence"
            else:
                fallback_reason = "target_base_moved"

            if stored is not None:
                elog.warning(
                    phase,
                    "ci_evidence_rejected",
                    "Persisted CI evidence exists but is not authoritative for this integration",
                    rejection_reason=fallback_reason,
                    checkpoint_sha=checkpoint_sha,
                    ci_status=stored.get("status"),
                    run_id=stored.get("run_id"),
                    recorded_base_sha=stored.get("base_sha"),
                    integration_base_sha=integration_base or None,
                )

            elog.info(
                phase,
                "ci_evidence_live_fallback",
                "Persisted CI evidence unusable; falling back to a live GitHub lookup",
                fallback_reason=fallback_reason,
                checkpoint_sha=checkpoint_sha or None,
            )
            verification = self.github_work_tracking.wait_for_ci(
                experiment_id,
                start_timeout_seconds=0,
                completion_timeout_seconds=0,
                poll_interval_seconds=0.01,
            )
            # `reason` is where "GitHub CI identity unavailable: configure
            # GITHUB_TOKEN/GH_TOKEN" surfaces. Logging it here is what makes
            # a credential problem readable as itself instead of arriving
            # later disguised as a generic adoption failure (#141).
            level = "info" if verification.status == "passed" else "warning"
            getattr(elog, level)(
                phase,
                "ci_evidence_live_result",
                "Live GitHub CI lookup finished",
                source="live",
                ci_status=verification.status,
                commit_sha=verification.commit_sha or None,
                conclusion=verification.conclusion,
                run_id=verification.run_id,
                reason=verification.reason,
                credentials_available=bool(
                    getattr(self.github_work_tracking, "configured", True)
                ),
            )

        exact = bool(checkpoint_sha and verification.commit_sha == checkpoint_sha)
        if not exact and verification.status in {"passed", "failed"}:
            # An authoritative verdict that belongs to a different commit is
            # a distinct situation from having none: the run exists, it just
            # does not verify the tree adoption is about to integrate.
            elog.warning(
                phase,
                "ci_evidence_rejected",
                "CI result does not verify the exact checkpoint and cannot be used",
                rejection_reason="checkpoint_mismatch",
                source=evidence_source,
                checkpoint_sha=checkpoint_sha or None,
                commit_sha=verification.commit_sha or None,
                ci_status=verification.status,
                run_id=verification.run_id,
            )
        elif exact and verification.status == "failed":
            # CI verified this exact tree and rejected it. Logged here, at
            # the one place that resolves a verdict, so it reads the same
            # whether the row came from the store or from a live lookup -
            # and so no consumer has to repeat it.
            elog.warning(
                phase,
                "ci_evidence_rejected",
                "Authoritative CI verified this exact checkpoint and failed it",
                rejection_reason="red_verdict",
                source=evidence_source,
                checkpoint_sha=checkpoint_sha,
                ci_status=verification.status,
                conclusion=verification.conclusion,
                run_id=verification.run_id,
                observed_at=(stored or {}).get("observed_at"),
            )

        return AuthoritativeCI(
            verification=verification,
            checkpoint_sha=checkpoint_sha,
            exact=exact,
        )
