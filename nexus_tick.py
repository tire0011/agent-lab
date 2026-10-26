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
import threading

from modules.logging_config import configure_logging
from modules.nexus_initiative import NexusInitiativeService
from modules.nexus_task_store import NexusTaskStore
from modules.run_store import DEFAULT_DB_PATH

configure_logging()
logger = logging.getLogger("nexus_tick")

_ISSUE_LIMIT = 100


def _list_open_issues(repo_root: str) -> list[dict]:
    """Return the open GitHub issue backlog available to Nexus.

    The initiative budget and one-at-a-time gate are the safety boundary.  A
    label gate made the supposedly self-directed tick depend on a human first
    labelling every issue, and in production that left it with no work to
    plan. Pull a bounded view of the ordinary issue backlog instead. Pull
    requests are excluded by ``gh issue list`` and linked issues are filtered
    by :class:`NexusInitiativeService` before dispatch.
    """
    try:
        proc = subprocess.run(
            [
                "gh", "issue", "list",
                "--state", "open",
                "--limit", str(_ISSUE_LIMIT),
                "--json", "number,title,body,labels",
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


def _gh(repo_root: str, argv: list[str]) -> None:
    """Run one ``gh`` write for the planning step; raise on failure."""
    proc = subprocess.run(
        ["gh", *argv], cwd=repo_root, capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:400] or f"gh exited {proc.returncode}")


def _comment_on_issue(repo_root: str, number: int, text: str) -> None:
    _gh(repo_root, ["issue", "comment", str(number), "--body", text])


def _open_proposal(repo_root: str, title: str, body: str) -> None:
    from modules.nexus_planner import PROPOSAL_LABEL

    _gh(repo_root, [
        "label", "create", PROPOSAL_LABEL, "--force", "--color", "FBCA04",
        "--description", "Suggested by Nexus; not worked on until a human removes this label",
    ])
    footer = "\n\n---\nSuggested by the Nexus planning step. Remove the `proposal` label to let Nexus work on it."
    _gh(repo_root, ["issue", "create", "--title", title, "--body", body + footer, "--label", PROPOSAL_LABEL])


def _count_open_proposals(repo_root: str) -> int:
    """All open proposals; the issue list itself stops at ``_ISSUE_LIMIT``."""
    from modules.nexus_planner import PROPOSAL_LABEL

    proc = subprocess.run(
        ["gh", "issue", "list", "--state", "open", "--label", PROPOSAL_LABEL,
         "--limit", "1000", "--json", "number"],
        cwd=repo_root, capture_output=True, text=True, timeout=60, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:400] or f"gh exited {proc.returncode}")
    return len(json.loads(proc.stdout or "[]"))


def _planner():
    """The planning step runs on Azure, like the controller (#337, #354)."""
    import asyncio

    from modules.nexus_delegation import NexusDelegationService
    from modules.nexus_planner import IssuePlanner

    def complete(prompt: str) -> str:
        service = NexusDelegationService()
        try:
            result = asyncio.run(service.azure_chat(prompt, max_tokens=2000)).to_dict()
        finally:
            service.close()
        if not result.get("success"):
            raise RuntimeError(str((result.get("metadata") or {}).get("error") or "azure failed"))
        return str(result.get("content") or "")

    return IssuePlanner(complete)


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
        planner=_planner() if controller else None,
        issue_commenter=lambda number, text: _comment_on_issue(args.repo_root, number, text),
        proposal_creator=lambda title, body: _open_proposal(args.repo_root, title, body),
        proposal_counter=lambda: _count_open_proposals(args.repo_root),
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


def _wait_for_experiments() -> None:
    """Keep the process alive until the work it started has ended (#358).

    The controller runs an experiment, and the adoption service an
    integration, in a daemon thread of this process; the oneshot unit would
    otherwise exit and kill it while it is still queued or adopting.
    """
    from modules.experiment_adoption_service import ADOPTION_THREAD_PREFIX
    from modules.nexus_controller_service import EXPERIMENT_THREAD_PREFIX

    for thread in threading.enumerate():
        if thread.name.startswith((EXPERIMENT_THREAD_PREFIX, ADOPTION_THREAD_PREFIX)):
            logger.info("waiting for %s to finish", thread.name)
            thread.join()


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
            resume = service.resume_blocked_capability()
            if resume.decision == "dispatched":
                logger.info(
                    "resumed blocked controller session %s",
                    resume.candidate.ref if resume.candidate else "?",
                )
                return 0
            if resume.decision in {"busy", "budget_exhausted", "error"}:
                logger.info("capability resume not started (%s): %s", resume.decision, resume.detail)
                return 0 if resume.decision != "error" else 1

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
        _wait_for_experiments()
        service.close()


if __name__ == "__main__":
    sys.exit(main())
