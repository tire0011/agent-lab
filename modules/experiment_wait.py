"""Nexus pauses on its experiment and continues with the result (#263 step 3): when
the experiment ends, its session is continued with one observation (status, PR
number, short reason on failure). A restart loses that moment, so the caller's
cheap poll resumes whatever the event missed.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from modules.experiment_service.models import _TERMINAL_STATUSES
from modules.redaction import redact_secrets

logger = logging.getLogger(__name__)

_MAX_REASON_CHARS = 500


def experiment_result(experiment: Any, work_ref: Any | None = None) -> dict[str, Any]:
    """The observation Nexus continues with after its experiment ended."""
    status = str(getattr(experiment, "status", ""))
    reason: str | None = None
    if status != "succeeded":
        exit_code = getattr(experiment, "test_exit_code", None)
        if getattr(experiment, "error", None):
            reason = str(experiment.error)
        elif exit_code not in (None, 0):
            tail = str(getattr(experiment, "test_output", "") or "")[-300:]
            reason = f"tests failed (exit {exit_code}): {tail}".strip()
        else:
            reason = status
        reason = redact_secrets(reason)[:_MAX_REASON_CHARS]
    return {
        "tool": "new_experiment",
        "experiment_id": str(getattr(experiment, "id", "")),
        "status": status,
        "pr_number": getattr(work_ref, "pr_number", None) if work_ref is not None else None,
        "reason": reason,
    }


def resume_after_experiment(
    orchestrator: Any,
    experiment_service: Any,
    github_work_tracking: Any | None,
    experiment_id: str,
) -> list[Any]:
    """Continue the sessions parked on *experiment_id* if it has ended."""
    experiment = experiment_service.get_experiment(experiment_id)
    if experiment.status not in _TERMINAL_STATUSES:
        return []
    work_ref = None
    if github_work_tracking is not None:
        try:
            work_ref = github_work_tracking.for_experiment(experiment_id)
        except Exception:  # noqa: BLE001 - the PR number is optional here
            logger.warning("experiment_wait: no work ref for %s", experiment_id, exc_info=True)
    return orchestrator.resume_after_experiment(
        experiment_id, experiment_result(experiment, work_ref)
    )


def resume_with_new_orchestrator(
    orchestrator_factory: Callable[..., Any],
    experiment_service: Any,
    github_work_tracking: Any | None,
    experiment_id: str,
    **factory_kwargs: Any,
) -> list[Any]:
    """Resume with an orchestrator that lives only for this continuation."""
    orchestrator = orchestrator_factory(experiment_service=experiment_service, **factory_kwargs)
    try:
        decisions = resume_after_experiment(
            orchestrator, experiment_service, github_work_tracking, experiment_id
        )
    finally:
        close = getattr(orchestrator, "close", None)
        if close:
            close()
    for decision in decisions:
        # Recorded in the session; the caller carries it out if it can.
        logger.info(
            "experiment_wait: session=%s continued after experiment=%s action=%s",
            getattr(decision, "session_id", None),
            experiment_id,
            getattr(decision, "action", None),
        )
    return decisions


def run_then_resume(
    run: Callable[[], Any],
    resume: Callable[[], Any],
    experiment_id: str,
) -> None:
    """Run the experiment, then continue its Nexus session whatever happened."""
    try:
        run()
    finally:
        try:
            resume()
        except Exception:  # noqa: BLE001 - the poll picks it up again
            logger.exception("experiment_wait: resume failed for experiment %s", experiment_id)
