"""Nexus pauses on its experiment and continues with the result (#263 step 3): when
the experiment ends, its session is continued with one observation (status, PR
number, short reason on failure). A restart loses that moment, so the caller's
cheap poll resumes whatever the event missed.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Iterable

from modules.experiment_service.models import _TERMINAL_STATUSES
from modules.redaction import redact_secrets

logger = logging.getLogger(__name__)

_MAX_REASON_CHARS = 500

# Who owns a waiting session, by its source type: each owner resumes only its
# own sessions, with its own continuation (#263). Everything else - issues,
# ideas, discussions - belongs to the generic controller.
MAILBOX_SOURCE_TYPES = ("task",)
CHAT_SOURCE_TYPES = ("chat",)
CONTROLLER_EXCLUDED_SOURCE_TYPES = MAILBOX_SOURCE_TYPES + CHAT_SOURCE_TYPES


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
    *,
    experiment_tool: Any | None = None,
    **factory_kwargs: Any,
) -> list[Any]:
    """Resume with an orchestrator that lives only for this continuation.

    With *experiment_tool* the resumed Nexus can start its next experiment
    itself, for example a retry after a failed one (#263 step 4).
    """
    orchestrator = orchestrator_factory(experiment_service=experiment_service, **factory_kwargs)
    if experiment_tool is not None:
        orchestrator.experiment_tool = experiment_tool
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
    *,
    fail: Callable[[str], Any] | None = None,
) -> None:
    """Run the experiment, then continue its Nexus session whatever happened.

    If *run* raises, *fail* first moves the experiment to a terminal status
    with a short reason; otherwise the session would only resume once the
    record became terminal, which a crashed runner never does.
    """
    try:
        run()
    except BaseException as exc:
        logger.exception("experiment_wait: experiment %s runner raised", experiment_id)
        if fail is not None:
            try:
                fail(redact_secrets(f"experiment runner raised {type(exc).__name__}: {exc}")[:_MAX_REASON_CHARS])
            except Exception:  # noqa: BLE001 - the poll still sees the session
                logger.exception("experiment_wait: could not fail experiment %s", experiment_id)
        if not isinstance(exc, Exception):
            raise
    finally:
        try:
            resume()
        except Exception:  # noqa: BLE001 - the poll picks it up again
            logger.exception("experiment_wait: resume failed for experiment %s", experiment_id)


def fail_experiment(experiment_service: Any, experiment_id: str, reason: str) -> bool:
    """Terminalise a non-terminal experiment with *reason*, if the service can."""
    fail = getattr(experiment_service, "fail_unfinished_experiment", None)
    if not callable(fail):
        return False
    try:
        return bool(fail(experiment_id, reason))
    except Exception:  # noqa: BLE001 - callers fall back to the poll
        logger.exception("experiment_wait: could not fail experiment %s", experiment_id)
        return False


def finished_waiting_experiments(
    task_store: Any,
    experiment_service: Any,
    *,
    source_types: Iterable[str] | None = None,
    exclude_source_types: Iterable[str] = (),
) -> list[str]:
    """Experiments that ended while sessions of the given types still wait on them.

    The durable half of the pause (#263): the waiting session is a row in the
    task store, so this finds it after a restart or a lost callback. Each
    owner (mailbox, chat, controller) asks only for its own source types so
    it resumes the session with its own continuation.
    """
    wanted = set(source_types) if source_types is not None else None
    excluded = set(exclude_source_types)
    ids: list[str] = []
    for session in task_store.sessions_waiting_for_experiments():
        experiment_id = getattr(session, "waiting_experiment_id", None)
        source_type = getattr(session, "source_type", None)
        if not experiment_id or experiment_id in ids:
            continue
        if wanted is not None and source_type not in wanted:
            continue
        if source_type in excluded:
            continue
        try:
            status = experiment_service.get_experiment(experiment_id).status
        except Exception:  # noqa: BLE001 - one unreadable record must not stop the rest
            logger.warning("experiment_wait: cannot read experiment %s", experiment_id, exc_info=True)
            continue
        if status in _TERMINAL_STATUSES:
            ids.append(str(experiment_id))
    return ids


def resume_finished_experiments(
    task_store: Any,
    experiment_service: Any,
    resume: Callable[[str], Any],
    *,
    source_types: Iterable[str] | None = None,
    exclude_source_types: Iterable[str] = (),
) -> list[str]:
    """Continue every waiting session of the given types whose experiment ended.

    Safe next to the event path: ``claim_waiting_for_experiment`` lets only
    one of them resume a session. Returns the experiment ids handed to
    *resume*.
    """
    resumed: list[str] = []
    for experiment_id in finished_waiting_experiments(
        task_store,
        experiment_service,
        source_types=source_types,
        exclude_source_types=exclude_source_types,
    ):
        try:
            resume(experiment_id)
        except Exception:  # noqa: BLE001 - retried next cycle
            logger.exception("experiment_wait: resume after experiment %s failed", experiment_id)
            continue
        resumed.append(experiment_id)
    return resumed


class ExperimentResumeWorker(threading.Thread):
    """Run the given recovery steps at startup and then every *interval* seconds."""

    def __init__(self, steps: Iterable[Callable[[], Any]], interval: float = 30.0) -> None:
        super().__init__(daemon=True, name="nexus-experiment-resume")
        self._steps = list(steps)
        self._interval = interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run_once(self) -> None:
        for step in self._steps:
            try:
                step()
            except Exception:  # noqa: BLE001 - next cycle retries
                logger.exception("experiment_wait: recovery step failed")

    def run(self) -> None:
        self.run_once()
        while not self._stop_event.wait(self._interval):
            self.run_once()
