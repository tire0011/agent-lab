"""One place that starts a Nexus ``new_experiment`` (#659, part A of #263).

Chat, mailbox and controller each repeated the same steps: put the handover into
the goal, track the work on GitHub, create the experiment, bind it to the issue,
park the Nexus session on it and run it in the background, then continue the
session with the result. :class:`ExperimentLauncher` does exactly these steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

from modules.experiment_handover import with_handover
from modules.experiment_service import ExperimentAlreadyActiveError
from modules.experiment_wait import run_then_resume

STATUS_SUBMITTED = "submitted"
STATUS_ALREADY_ACTIVE = "already_active"


@dataclass(slots=True)
class LaunchResult:
    experiment_id: str
    # ``submitted`` when started; for ``already_active`` the active experiment's status.
    status: str
    already_active: bool = False
    work_ref: Any | None = None


class ExperimentLauncher:
    """Start an experiment for a Nexus session and continue the session after it.

    ``background_starter(name, target)`` runs *target* off the caller's thread;
    ``resume_factory(experiment_id)`` continues the parked session(s) once the
    experiment has ended.
    """

    def __init__(
        self,
        experiment_service: Any,
        github_work_tracking: Any,
        background_starter: Callable[[str, Callable[[], None]], None],
        resume_factory: Callable[[str], Any],
    ) -> None:
        self.experiment_service = experiment_service
        self.github_work_tracking = github_work_tracking
        self.background_starter = background_starter
        self.resume_factory = resume_factory

    def launch(
        self,
        session_id: str | None,
        goal: str,
        handover: str | None,
        source: Any,
        *,
        task_store: Any | None = None,
        create_kwargs: dict[str, Any] | None = None,
    ) -> LaunchResult:
        """Run the launch steps in order.

        *source* carries ``source_type``, ``source_id`` and ``title`` for GitHub
        work tracking; its ``content`` defaults to the goal with the handover.
        *task_store* parks *session_id* on the experiment before it starts.
        *create_kwargs* are passed to ``create_experiment`` unchanged.
        GitHub tracking errors propagate before any experiment is created.
        """
        full_goal = with_handover(goal, handover)
        content = getattr(source, "content", None)
        lifecycle_source = SimpleNamespace(
            source_type=getattr(source, "source_type", None),
            source_id=getattr(source, "source_id", None),
            title=getattr(source, "title", None),
            content=full_goal if content is None else content,
        )
        work_ref = self.github_work_tracking.ensure_for_source(lifecycle_source)
        try:
            experiment = self.experiment_service.create_experiment(
                full_goal, **(create_kwargs or {})
            )
        except ExperimentAlreadyActiveError as exc:
            return LaunchResult(
                experiment_id=exc.experiment_id,
                status=exc.status,
                already_active=True,
                work_ref=work_ref,
            )
        experiment_id = experiment.id
        work_ref = self.github_work_tracking.bind_experiment(work_ref, experiment_id)
        # Nexus pauses on the experiment and continues the moment it ends (#263).
        if session_id and task_store is not None:
            task_store.wait_for_experiment(session_id, experiment_id)
        self.background_starter(
            experiment_id,
            lambda: run_then_resume(
                lambda: self.experiment_service.run_experiment(
                    experiment_id,
                    github_work_tracking=self.github_work_tracking,
                ),
                lambda: self.resume_factory(experiment_id),
                experiment_id,
            ),
        )
        return LaunchResult(
            experiment_id=experiment_id,
            status=STATUS_SUBMITTED,
            already_active=False,
            work_ref=work_ref,
        )
