#!/usr/bin/env python3
"""Nexus initiative tick — decide and start one piece of Nexus's own work.

This is the entry point that makes Nexus self-directed. Everything else in the
system waits to be asked: the mailbox timer waits for a human to write a file,
the chat waits for someone to type, the task worker waits for an approval. This
runs on a timer, looks at Nexus's own state, picks the single most useful thing
to do next, records why, and hands it to the existing pipeline.

The services are built exactly as ``api.py`` builds them, so a tick observes the
same lifecycle rules as every other caller.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys

from modules.logging_config import configure_logging
from modules.nexus_initiative import NexusInitiativeService
from modules.nexus_task_store import NexusTaskStore
from modules.run_store import DEFAULT_DB_PATH

configure_logging()
logger = logging.getLogger("nexus_tick")

_ISSUE_LABEL = "nexus"
_ISSUE_LIMIT = 20


def _list_open_issues(repo_root: str) -> list[dict]:
    """Open GitHub issues Nexus is allowed to pick up on its own.

    Deliberately label-gated: an issue only becomes self-startable work once a
    human has marked it, so opening an issue is not the same as dispatching an
    agent at it. Uses the ``gh`` CLI already installed and authenticated on the
    VM rather than adding another GitHub client.
    """
    try:
        proc = subprocess.run(
            [
                "gh", "issue", "list",
                "--state", "open",
                "--label", _ISSUE_LABEL,
                "--limit", str(_ISSUE_LIMIT),
                "--json", "number,title,body",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("issue lookup failed: %s", exc)
        return []
    if proc.returncode != 0:
        logger.warning("issue lookup failed: %s", proc.stderr.strip()[:400])
        return []
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        logger.warning("issue lookup returned unparsable JSON")
        return []


def _build_service(args) -> NexusInitiativeService:
    from modules.experiment_adoption_service import ExperimentAdoptionService
    from modules.experiment_service import ExperimentService
    from modules.nexus_controller_service import NexusControllerService
    from modules.operations_registry import OperationsRegistry
    from modules.task_orchestrator import NexusTaskOrchestrator

    experiment_service = ExperimentService(db_path=args.db_path)
    adoption_service = ExperimentAdoptionService(experiment_service=experiment_service)
    controller = None
    if not args.integration_only:
        registry = OperationsRegistry(experiment_service=experiment_service)
        controller = NexusControllerService(
            orchestrator=NexusTaskOrchestrator(
                operations_registry=registry,
                experiment_service=experiment_service,
            )
        )
    return NexusInitiativeService(
        db_path=args.db_path,
        experiment_service=experiment_service,
        adoption_service=adoption_service,
        controller=controller,
        issue_lister=(lambda: _list_open_issues(args.repo_root)) if controller else None,
        daily_budget=args.daily_budget,
    )


def _reconcile_stale_claims(db_path: str) -> None:
    """Release controller claims whose worker stopped heartbeating (#162).

    The tick runs on a timer, which makes it the natural place to notice a dead
    worker: the API only reconciles at startup, and a worker that dies while
    the API keeps running would otherwise hold its claim until the next
    restart. Never resumes the work and never fails the tick - a tick that
    could not reap is still a tick that can decide.
    """
    store = None
    try:
        store = NexusTaskStore(db_path=db_path)
        released = store.reconcile_stale_heartbeats()
        for session in released:
            logger.warning(
                "released stale claim: session=%s worker=%s status=%s",
                session.id, session.worker_id, session.status,
            )
    except Exception as exc:  # noqa: BLE001 - reaping must not break a tick
        logger.warning("could not reconcile stale controller claims: %s", exc)
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--repo-root", default="/agent")
    parser.add_argument("--daily-budget", type=int, default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be started without starting it",
    )
    parser.add_argument(
        "--integration-only",
        action="store_true",
        help="only drain unintegrated verified work; never start new work "
             "(no LLM controller is constructed)",
    )
    args = parser.parse_args(argv)

    # --dry-run reports what would happen and changes nothing, reaping
    # included.
    if not args.dry_run:
        _reconcile_stale_claims(args.db_path)

    service = _build_service(args)
    try:
        if not args.dry_run and service.controller is not None:
            try:
                resumed = service.controller.resume_available_capabilities()
            except Exception as exc:  # noqa: BLE001 - do not start new work beside a failed resume
                logger.error("blocked capability resume failed: %s", exc)
                return 1
            if resumed:
                logger.info(
                    "resumed %d blocked controller session(s): %s",
                    len(resumed),
                    ", ".join(
                        str(decision.session_id)
                        for decision in resumed
                        if decision.session_id
                    ),
                )
                return 0

        for entry in service.abandoned_work():
            logger.warning(
                "abandoned rows left in a running state: %s x%s",
                entry["table"], entry["count"],
            )

        if args.dry_run:
            busy = service.busy_reason()
            if busy:
                logger.info("would do nothing: %s", busy)
                return 0
            candidates = service.candidates()
            if not candidates:
                logger.info("would do nothing: no actionable work")
                return 0
            chosen = candidates[0]
            logger.info(
                "would start [%s] %s - %s (%d candidate(s) total)",
                chosen.kind, chosen.ref, chosen.reason, len(candidates),
            )
            return 0

        decision = service.tick()
        if decision.acted and decision.candidate is not None:
            logger.info(
                "started [%s] %s - %s",
                decision.candidate.kind, decision.candidate.ref, decision.candidate.reason,
            )
        else:
            logger.info("no work started (%s): %s", decision.decision, decision.detail)
        return 0 if decision.decision != "error" else 1
    finally:
        service.close()


if __name__ == "__main__":
    sys.exit(main())
