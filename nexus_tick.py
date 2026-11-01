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
import os
import sqlite3
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime, timezone

from modules.logging_config import configure_logging
from modules.nexus_initiative import NexusInitiativeService
from modules.nexus_task_store import NexusTaskStore
from modules.run_store import DEFAULT_DB_PATH

configure_logging()
logger = logging.getLogger("nexus_tick")

_ISSUE_LIMIT = 100

# The exploring planner (#384) reads code and calls tools; bound it.
_PLANNER_TIMEOUT_SECONDS = 900
_PLANNER_STALL_SECONDS = 300

# Things worth telling Claude about, collected during one tick (#365).
_events: list[str] = []


def _service_diagnostics() -> dict:
    url = os.environ.get(
        "NEXUS_DIAGNOSTICS_URL", "http://127.0.0.1:8000/diagnostics/public"
    )
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - fixed/operator URL
        return json.loads(response.read().decode("utf-8"))


def _restart_stale_service() -> None:
    from modules.task_finalization import TaskFinalizationCoordinator

    TaskFinalizationCoordinator._restart_services(
        TaskFinalizationCoordinator._restart_service_names()
    )


def _checkout_revision(repo_root: str) -> str:
    """Read the revision of the checkout executing this initiative tick."""
    proc = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return proc.stdout.strip()


def _main_commit_date(repo_root: str) -> str:
    """Read the date of the newest commit on the local ``origin/main`` ref."""
    proc = subprocess.run(
        ["git", "log", "-1", "--format=%cI", "origin/main"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return proc.stdout.strip()


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

    # The label exists in the repository; the VM's gh has no `label` command.
    footer = "\n\n---\nSuggested by the Nexus planning step. Remove the `proposal` label to let Nexus work on it."
    _gh(repo_root, ["issue", "create", "--title", title, "--body", body + footer, "--label", PROPOSAL_LABEL])
    _events.append(f"New proposal issue: {title}")


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


def _explore_with_claude(repo_root: str, prompt: str) -> str:
    """Plan as a read-only Claude agent with the Nexus MCP read tools (#384)."""
    import tempfile

    from modules.claude_worker import ClaudeWorker
    from modules.nexus_mcp_tools import READ_ONLY_TOOLS, WRITE_TOOLS

    key = os.environ.get("AGENT_API_KEY", "")
    if not key:
        raise RuntimeError("AGENT_API_KEY is not set")
    config = {"mcpServers": {"nexus": {
        "type": "http",
        "url": os.environ.get("NEXUS_MCP_URL", "http://127.0.0.1:8000/mcp/"),
        "headers": {"x-api-key": key},
    }}}
    allowed = ",".join(f"mcp__nexus__{name}" for name in READ_ONLY_TOOLS)
    # Not pre-approved is already denied; naming the write tools makes it explicit.
    denied = ",".join(f"mcp__nexus__{name}" for name in WRITE_TOOLS)
    with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
        json.dump(config, handle)
        handle.flush()
        result = ClaudeWorker().run(
            task=prompt,
            working_dir=repo_root,
            sandbox="read-only",
            timeout_seconds=_PLANNER_TIMEOUT_SECONDS,
            stall_seconds=_PLANNER_STALL_SECONDS,
            phase="planning",
            extra_args=[
                "--mcp-config", handle.name,
                "--allowedTools", allowed,
                "--disallowedTools", denied,
            ],
        )
    if not result.success:
        raise RuntimeError(result.error or "claude planning failed")
    return result.final_message or ""


def _planner(repo_root: str):
    """Plan as an exploring agent; a single Azure call is the fallback (#337, #354, #384)."""
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

    return IssuePlanner(complete, explore=lambda prompt: _explore_with_claude(repo_root, prompt))


def _build_service(args) -> NexusInitiativeService:
    from modules.experiment_adoption_service import ExperimentAdoptionService
    from modules.experiment_service import ExperimentService
    from modules.nexus_controller_service import NexusControllerService
    from modules.operations_registry import OperationsRegistry
    from modules.task_orchestrator import NexusTaskOrchestrator

    experiment_service = ExperimentService(db_path=args.db_path)
    from modules.nexus_reviewer import default_reviewer

    adoption_service = ExperimentAdoptionService(
        experiment_service=experiment_service, reviewer=default_reviewer(args.db_path),
    )
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
        planner=_planner(args.repo_root) if controller else None,
        issue_commenter=lambda number, text: _comment_on_issue(args.repo_root, number, text),
        proposal_creator=lambda title, body: _open_proposal(args.repo_root, title, body),
        proposal_counter=lambda: _count_open_proposals(args.repo_root),
        diagnostics_client=_service_diagnostics,
        checkout_revision=lambda: _checkout_revision(args.repo_root),
        main_commit_date=lambda: _main_commit_date(args.repo_root),
        restart_hook=_restart_stale_service,
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


def _cleanup_expired_worktrees(service) -> None:
    """Remove worktrees of terminal experiments past their retention (#376).

    The API only cleans up at startup; the tick is what keeps a long-running
    deployment from filling its disk. Never fails the tick.
    """
    experiment_service = getattr(service, "experiment_service", None)
    if experiment_service is None:
        return
    try:
        result = experiment_service.cleanup_expired_worktrees()
        if result.removed:
            logger.info("removed %d expired experiment worktree(s)", len(result.removed))
        for experiment_id, reason in result.errors.items():
            logger.warning("could not remove worktree of experiment %s: %s", experiment_id, reason)
    except Exception as exc:  # noqa: BLE001 - cleanup must not break a tick
        logger.warning("could not clean up expired experiment worktrees: %s", exc)


def _wait_for_experiments() -> None:
    """Keep the process alive until the work it started has ended (#358).

    The controller runs an experiment, and the adoption service an
    integration, in a daemon thread of this process; the oneshot unit would
    otherwise exit and kill it while it is still queued or adopting.
    """
    from modules.experiment_adoption_service import ADOPTION_THREAD_PREFIX
    from modules.nexus_controller_service import EXPERIMENT_THREAD_PREFIX

    # A thread can start the next one - a controller session that resumes
    # after a rejected experiment starts a new one (#388) - so look again
    # until none is left.
    while True:
        pending = [
            thread for thread in threading.enumerate()
            if thread.is_alive() and thread.name.startswith((EXPERIMENT_THREAD_PREFIX, ADOPTION_THREAD_PREFIX))
        ]
        if not pending:
            return
        for thread in pending:
            logger.info("waiting for %s to finish", thread.name)
            thread.join()


def _finished_badly(db_path: str, since: str) -> list[str]:
    """Experiments and adoptions that ended without success since *since*."""
    found: list[str] = []
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return found
    try:
        for exp_id, status, goal, error in db.execute(
            "SELECT id, status, goal, error FROM experiments "
            "WHERE ended_at >= ? AND status != 'succeeded'",
            (since,),
        ):
            lines = str(goal or "").strip().splitlines()
            title = lines[0][:100] if lines else ""
            found.append(f"Experiment {exp_id[:8]} ({title}) ended {status}: {str(error or '')[:300]}")
        for exp_id, status, reason in db.execute(
            "SELECT experiment_id, status, reason FROM experiment_adoptions "
            "WHERE updated_at >= ? AND status IN ('failed', 'needs_review')",
            (since,),
        ):
            found.append(f"Adoption of experiment {exp_id[:8]} ended {status}: {str(reason or '')[:300]}")
    except sqlite3.Error as exc:
        logger.warning("notify: could not read outcomes: %s", exc)
    finally:
        db.close()
    return found


def _notify(repo_root: str, messages: list[str]) -> None:
    """Comment on the watched notification PR so Claude wakes up (#365).

    ``NEXUS_NOTIFY_PR`` is ``owner/repo#number``; unset means no notifications.
    Best effort: a failed comment must not fail the tick.
    """
    target = os.environ.get("NEXUS_NOTIFY_PR", "").strip()
    if not target or not messages or "#" not in target:
        return
    repo, number = target.rsplit("#", 1)
    body = "Nexus:\n\n" + "\n".join(f"- {m}" for m in messages)
    try:
        _gh(repo_root, ["pr", "comment", number, "--repo", repo, "--body", body])
    except Exception as exc:  # noqa: BLE001
        logger.warning("notify: comment failed: %s", exc)


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
    started_at = datetime.now(timezone.utc).isoformat()
    _events.clear()
    decision = None

    # --dry-run reports what would happen and changes nothing, reaping
    # included.
    if not args.dry_run:
        _reconcile_stale_claims(args.db_path)
        from modules.main_guard import guard_main

        reverted = guard_main(args.repo_root)
        if reverted:
            logger.warning("main guard: %s", reverted)
            _events.append(reverted)

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
            candidates = service.candidates(record_skips=False)
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
        if not args.dry_run:
            _cleanup_expired_worktrees(service)
        service.close()
        if not args.dry_run:
            if decision is not None and decision.decision == "error":
                _events.append(f"Tick error: {str(decision.detail or '')[:300]}")
            _notify(args.repo_root, _events + _finished_badly(args.db_path, started_at))


if __name__ == "__main__":
    sys.exit(main())
