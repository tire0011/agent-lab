"""``new_experiment`` as Nexus' own tool (#263 step 4).

Before this, ``new_experiment`` was an end point: the controller stopped and
whoever had called Nexus - the mailbox, the controller service, the chat -
created and started the experiment in its own way. Now Nexus carries it out
itself, through this tool:

1. GitHub work tracking (issue + branch) for the session's source,
2. the experiment, whose goal is the session's task plus the handover of what
   Nexus already found (``experiment_handover``),
3. the session parked on the experiment, so it holds neither a claim nor the
   local model while the experiment runs,
4. the run in the background, after which the caller's continuation resumes
   the same session with the result observation (``experiment_wait``).

Callers only configure the tool - how to run in the background, how to link
the experiment to their own record, how to continue afterwards - and report
the :class:`ExperimentLaunch` Nexus returns. None of them creates an
experiment any more.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from types import SimpleNamespace
from typing import Any, Callable

from modules.experiment_handover import with_handover
from modules.experiment_service import ExperimentAlreadyActiveError
from modules.experiment_wait import fail_experiment, run_then_resume
from modules.github_work_tracking import GitHubWorkTrackingError
from modules.redaction import redact_secrets

logger = logging.getLogger(__name__)

LAUNCH_SUBMITTED = "submitted"
LAUNCH_ALREADY_ACTIVE = "already_active"
LAUNCH_BLOCKED = "blocked"
LAUNCH_LIMIT_REACHED = "limit_reached"
LAUNCH_NOT_LINKED = "not_linked"
LAUNCH_NOT_PARKED = "not_parked"
LAUNCH_NOT_STARTED = "not_started"

# Kind of the session event that records a launch. Deliberately not
# ``new_experiment``: those events are the results Nexus resumes with, and
# their count is the number of experiments that already ended.
LAUNCH_EVENT_KIND = "experiment_launch"

GOAL_CHAR_CAP = 24000
GOAL_TITLE_CAP = 200
GOAL_TRUNCATION_MARKER = "\n\n[... earlier text truncated ...]\n\n"


@dataclass(slots=True)
class ExperimentLaunch:
    """What Nexus' ``new_experiment`` tool did."""

    status: str
    experiment_id: str | None = None
    experiment_status: str | None = None
    issue_number: int | None = None
    branch_name: str | None = None
    reason: str | None = None

    @property
    def started(self) -> bool:
        return self.status == LAUNCH_SUBMITTED

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def _normalise_input(text: str) -> str:
    return " ".join(text.split())


def _drop_repeated_inputs(inputs: list[str]) -> list[str]:
    """Drop inputs whose normalised text a later input equals or starts with."""
    normalised = [_normalise_input(item) for item in inputs]
    return [
        item
        for index, item in enumerate(inputs)
        if not any(later.startswith(normalised[index]) for later in normalised[index + 1:])
    ]


def _keep_title_and_tail(goal: str, first_input: str) -> str:
    """Cut an over-long goal from the front: the newest text is at the end."""
    title = first_input.splitlines()[0][:GOAL_TITLE_CAP]
    head = title + GOAL_TRUNCATION_MARKER
    return head + goal[-(GOAL_CHAR_CAP - len(head)):]


def session_goal(task_store: Any, session_id: str, fallback: str = "") -> str:
    """The user-visible task of a session across clarification replies.

    Tool observations are not copied verbatim; what Nexus learned travels in
    the handover instead.
    """
    try:
        stored_inputs = task_store.user_inputs(session_id)
    except Exception:  # noqa: BLE001 - a missing journal falls back to the turn input
        return fallback
    user_inputs = [str(content).strip() for content in stored_inputs]
    user_inputs = [item for item in user_inputs if item]
    if not user_inputs:
        return fallback
    # #495: a retried issue stores the whole body again, so an input that a
    # later input repeats (or starts with) adds nothing but tokens.
    kept = _drop_repeated_inputs(user_inputs)
    dropped = len(user_inputs) - len(kept)
    goal = "\n\nUser follow-up:\n".join(kept)
    if len(goal) <= GOAL_CHAR_CAP:
        return goal
    try:
        task_store.append_event(
            session_id,
            role="tool",
            kind="goal_truncated",
            content=(
                f"Execution goal truncated from {len(goal)} to {GOAL_CHAR_CAP} "
                f"characters; {dropped} duplicate input(s) dropped."
            ),
        )
    except Exception:  # noqa: BLE001 - bookkeeping only
        pass
    return _keep_title_and_tail(goal, kept[0])


def start_in_thread(name: str, target: Callable[[], Any]) -> None:
    def _run() -> None:
        try:
            target()
        except Exception:  # noqa: BLE001 - the experiment records its own failure
            logger.exception("nexus_experiment_tool: experiment %s raised", name)

    threading.Thread(target=_run, daemon=True, name=f"nexus-exp-{name[:8]}").start()


class NexusExperimentTool:
    """Carry out a ``new_experiment`` decision on Nexus' behalf.

    *background* runs ``target`` off the controller turn (a daemon thread by
    default). *on_created* links the new experiment to the caller's own record
    before it runs and may refuse it by returning ``False``. *on_finished*
    continues the parked Nexus session once the experiment ended. *goal_for*
    builds the task text for a session; the handover is always appended.
    *max_experiments* bounds how many experiments one session may start.
    """

    def __init__(
        self,
        experiment_service: Any,
        github_work_tracking: Any,
        *,
        background: Callable[[str, Callable[[], Any]], None] | None = None,
        on_created: Callable[[Any, Any], bool | None] | None = None,
        on_finished: Callable[[str], Any] | None = None,
        on_event: Callable[[str, str, dict[str, Any]], None] | None = None,
        goal_for: Callable[[Any, Any, str], str] | None = None,
        max_experiments: int | None = None,
    ) -> None:
        self.experiment_service = experiment_service
        self.github_work_tracking = github_work_tracking
        self.background = background or start_in_thread
        self.on_created = on_created
        self.on_finished = on_finished
        self.on_event = on_event
        self.goal_for = goal_for
        self.max_experiments = max_experiments

    def _event(self, kind: str, message: str, **fields: Any) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, message, fields)
        except Exception:  # noqa: BLE001 - logging must not stop the experiment
            logger.debug("nexus_experiment_tool: on_event failed", exc_info=True)

    def _finished_experiments(self, task_store: Any, session_id: str) -> int:
        """Experiments of this session that already ended (their result events)."""
        if self.max_experiments is None:
            return 0
        try:
            return len(
                task_store.recent_events(
                    session_id,
                    role="tool",
                    kind="new_experiment",
                    limit=self.max_experiments + 1,
                )
            )
        except Exception:  # noqa: BLE001 - an unreadable journal does not lift the limit
            return self.max_experiments

    def start(self, task_store: Any, session: Any, decision: Any, fallback: str) -> ExperimentLaunch:
        if self.max_experiments is not None:
            finished = self._finished_experiments(task_store, session.id)
            if finished >= self.max_experiments:
                return ExperimentLaunch(
                    status=LAUNCH_LIMIT_REACHED,
                    reason=(
                        f"Nexus wanted another experiment; the limit of "
                        f"{self.max_experiments} experiments per task is reached."
                    ),
                )

        source_id = session.source_id or session.id
        # The experiment goal: the task followed by the handover (#263).
        task = (
            self.goal_for(task_store, session, fallback)
            if self.goal_for is not None
            else session_goal(task_store, session.id, fallback)
        ) or fallback
        goal = with_handover(task, getattr(decision, "experiment_handover", None))
        self._event("github_work_tracking_started", "starting GitHub work tracking")
        try:
            work_ref = self.github_work_tracking.ensure_for_source(
                SimpleNamespace(
                    source_type=session.source_type,
                    source_id=source_id,
                    title=session.title,
                    content=task,
                )
            )
        except GitHubWorkTrackingError as exc:
            self._event(
                "github_work_tracking_failed",
                "GitHub lifecycle blocked before implementation start",
                error=str(exc),
            )
            return ExperimentLaunch(
                status=LAUNCH_BLOCKED,
                reason=f"GitHub lifecycle blocked before implementation start: {exc}",
            )
        # Issue #184: make the chosen Issue visible at the moment it is chosen.
        self._event(
            "github_work_tracking_linked",
            f"work tracked on GitHub issue #{work_ref.issue_number}",
            issue_number=work_ref.issue_number,
            branch_name=work_ref.branch_name,
        )

        try:
            experiment = self.experiment_service.create_experiment(
                goal,
                complexity=getattr(decision, "complexity", None),
                source_type=session.source_type,
                source_id=source_id,
            )
        except ExperimentAlreadyActiveError as exc:
            self._event(
                "already_active",
                "Nexus matched an active experiment",
                experiment_id=exc.experiment_id,
                experiment_status=exc.status,
            )
            return ExperimentLaunch(
                status=LAUNCH_ALREADY_ACTIVE,
                experiment_id=exc.experiment_id,
                experiment_status=exc.status,
                issue_number=work_ref.issue_number,
                branch_name=work_ref.branch_name,
                reason=(
                    "an experiment for this GitHub issue is already active "
                    f"(experiment_id={exc.experiment_id}, status={exc.status})"
                ),
            )

        experiment_id = experiment.id
        launch = ExperimentLaunch(
            status=LAUNCH_SUBMITTED,
            experiment_id=experiment_id,
            issue_number=work_ref.issue_number,
            branch_name=work_ref.branch_name,
        )

        # From here on the experiment exists and is active. Every way out that
        # does not run it ends it with a reason, so it can never block later
        # experiments for the issue as an orphan.
        try:
            work_ref = self.github_work_tracking.bind_experiment(work_ref, experiment_id)
        except Exception as exc:  # noqa: BLE001 - ended below, reported to Nexus
            return self._abandon(
                launch,
                LAUNCH_BLOCKED,
                f"GitHub work tracking could not bind the experiment: {exc}",
            )
        launch.issue_number = work_ref.issue_number
        launch.branch_name = work_ref.branch_name

        try:
            linked = self.on_created is None or self.on_created(experiment, decision) is not False
        except Exception as exc:  # noqa: BLE001 - ended below, reported to Nexus
            return self._abandon(
                launch,
                LAUNCH_BLOCKED,
                f"the caller could not link the experiment: {exc}",
            )
        if not linked:
            logger.warning(
                "nexus_experiment_tool: caller did not link experiment %s; not started",
                experiment_id,
            )
            return self._abandon(
                launch, LAUNCH_NOT_LINKED, "the task was no longer waiting for an experiment"
            )
        self._event("experiment_created", "experiment created by Nexus", experiment_id=experiment_id)

        # Nexus pauses on the experiment and continues the moment it ends (#263).
        # Without a parked session nobody would resume with the result, so the
        # experiment does not run.
        try:
            parked = bool(task_store.wait_for_experiment(session.id, experiment_id))
        except Exception as exc:  # noqa: BLE001 - ended below, reported to Nexus
            return self._abandon(
                launch, LAUNCH_NOT_PARKED, f"the Nexus session could not pause: {exc}"
            )
        if not parked:
            return self._abandon(
                launch,
                LAUNCH_NOT_PARKED,
                "the Nexus session could not pause on the experiment (session not found)",
            )
        try:
            task_store.append_event(
                session.id,
                role="tool",
                kind=LAUNCH_EVENT_KIND,
                content=json.dumps(
                    {**launch.as_dict(), "goal": goal[:4000]}, separators=(",", ":")
                ),
                metadata={"experiment_id": experiment_id},
            )
        except Exception:  # noqa: BLE001 - the experiment record holds the goal too
            logger.debug("nexus_experiment_tool: could not journal launch", exc_info=True)

        on_finished = self.on_finished
        try:
            self.background(
                experiment_id,
                lambda: run_then_resume(
                    lambda: self.experiment_service.run_experiment(
                        experiment_id,
                        github_work_tracking=self.github_work_tracking,
                    ),
                    (lambda: on_finished(experiment_id)) if on_finished else (lambda: None),
                    experiment_id,
                    fail=lambda reason: fail_experiment(
                        self.experiment_service, experiment_id, reason
                    ),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - ended below, reported to Nexus
            # The session stays parked on the now-ended experiment; the durable
            # recovery resumes it with this failure as its observation.
            return self._abandon(
                launch, LAUNCH_NOT_STARTED, f"the experiment could not be started: {exc}"
            )
        return launch

    def _abandon(self, launch: ExperimentLaunch, status: str, reason: str) -> ExperimentLaunch:
        """End an experiment Nexus created but will not run, and say why."""
        reason = redact_secrets(reason)[:500]
        if launch.experiment_id:
            ended = fail_experiment(self.experiment_service, launch.experiment_id, reason)
            if not ended:
                logger.error(
                    "nexus_experiment_tool: could not end unstarted experiment %s",
                    launch.experiment_id,
                )
            launch.experiment_status = "failed" if ended else None
        launch.status = status
        launch.reason = reason
        self._event(
            "experiment_not_started",
            f"experiment not started: {reason}",
            experiment_id=launch.experiment_id,
            launch_status=status,
        )
        return launch
