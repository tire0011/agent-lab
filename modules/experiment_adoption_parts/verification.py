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
        """Authoritative GitHub CI for the exact verified checkpoint (#134) - the
        single definition shared by the pipeline and the stale-failure assessment:
        evidence counts only when it verifies the very commit adoption integrates
        (the SHA comparison rejects a verdict for another commit of the experiment).

        A verdict already accepted for that exact commit is read from the durable
        store first (#138) - re-deriving needs live credentials, and the fact must
        outlive the process that observed it (#136). Precedence: the most recent
        authoritative observation for the checkpoint SHA on the base we integrate
        onto wins; a live lookup runs only when no such verdict is stored and is
        written through. The base comes from the local ``origin/<target>`` ref (no
        credentials); a verdict recorded against any other base is not reused,
        because cherry-picking onto a moved base produces a tree CI never saw (#134).
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
                # `ci_evidence_reused` means an accepted, usable verdict; a red one
                # supports nothing and is reported as a rejection below - one piece
                # of evidence must never be logged as both accepted and rejected.
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
            # Why the store could not answer decides what the operator has to fix,
            # so name it rather than collapsing every case into "no evidence".
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
            # `reason` is where "configure GITHUB_TOKEN/GH_TOKEN" surfaces; logging
            # it here keeps a credential problem readable as itself instead of
            # arriving later disguised as a generic adoption failure (#141).
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
            # A verdict for a different commit is distinct from having none: the run
            # exists but does not verify the tree adoption is about to integrate.
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
            # CI verified this exact tree and rejected it; logged here, the one place
            # that resolves a verdict, so it reads the same for stored and live rows.
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
