#!/usr/bin/env python3
"""Nexus initiative tick - decide and start one piece of Nexus's own work.

Runs on a timer, looks at Nexus's own state, picks the single most useful
next step, records why, and hands it to the existing pipeline. Services are
built as ``api.py`` builds them.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

from modules import github_authorization
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
# How often the stall sweep (#400) runs while the tick waits for its experiment.
_STALL_SWEEP_INTERVAL_SECONDS = 5 * 60


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
    """The open GitHub issue backlog available to Nexus (no label gate).

    The budget and the one-at-a-time gate are the safety boundary; pull
    requests are excluded and linked issues are filtered before dispatch.
    """
    try:
        proc = subprocess.run(
            [
                "gh", "issue", "list",
                "--state", "open",
                "--limit", str(_ISSUE_LIMIT),
                # ``author`` decides whether the content-hash check applies
                # (trusted authors may edit their issue after approval).
                "--json", "number,title,body,labels,author",
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


def _open_part_issue(repo_root: str, title: str, body: str) -> int | None:
    """A part of a split issue (#626): no proposal label, it is ready to work on."""
    proc = subprocess.run(
        ["gh", "issue", "create", "--title", title, "--body", body],
        cwd=repo_root, capture_output=True, text=True, timeout=60, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:400] or f"gh exited {proc.returncode}")
    match = re.search(r"/issues/(\d+)", proc.stdout or "")
    _events.append(f"New part issue: {title}")
    return int(match.group(1)) if match else None


def _close_issue(repo_root: str, number: int, text: str) -> None:
    _gh(repo_root, ["issue", "close", str(number), "--comment", text])
    _events.append(f"Closed split issue #{number}: {text}")


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


def _owner_decisions(repo_root: str, days: int = 30, limit: int = 15) -> list[dict[str, str]]:
    """Proposals the owner closed lately, with the closing comment as the reason.

    A rejected proposal keeps its label; its last comment says why. The
    planner sees these so it does not suggest the same thing again.
    """
    from modules.nexus_planner import PROPOSAL_LABEL

    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    proc = subprocess.run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/issues?state=closed&labels={PROPOSAL_LABEL}"
         f"&since={since}&per_page={limit}"],
        cwd=repo_root, capture_output=True, text=True, timeout=60, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:400] or f"gh exited {proc.returncode}")
    decisions = []
    for issue in json.loads(proc.stdout or "[]")[:limit]:
        if issue.get("state_reason") == "completed":
            decisions.append({"title": str(issue.get("title") or ""), "reason": "implemented"})
            continue
        comments = subprocess.run(
            ["gh", "api", f"repos/{{owner}}/{{repo}}/issues/{issue['number']}/comments?per_page=100"],
            cwd=repo_root, capture_output=True, text=True, timeout=60, check=False,
        )
        bodies = json.loads(comments.stdout or "[]") if comments.returncode == 0 else []
        # Only comments whose API-verified author is a trusted actor count as
        # the owner's decision; comment text itself is never trusted.
        trusted_bodies = [
            c for c in bodies
            if isinstance(c, dict) and github_authorization.classify_comment(c).trusted
        ]
        reason = str(trusted_bodies[-1].get("body") or "") if trusted_bodies else ""
        reason = reason.split("\n---\n", 1)[0]
        decisions.append({
            "title": str(issue.get("title") or ""),
            "reason": " ".join(reason.split())[:400] or f"closed ({issue.get('state_reason') or 'no reason'})",
        })
    return decisions


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
        # Free registry models first (#552), Azure only when none answers.
        from modules import model_registry

        free = model_registry.chat_first(prompt, role="planning", max_cost="free", max_tokens=2000)
        if free.success:
            return free.content
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
        issue_creator=lambda title, body: _open_part_issue(args.repo_root, title, body),
        issue_closer=lambda number, text: _close_issue(args.repo_root, number, text),
        proposal_counter=lambda: _count_open_proposals(args.repo_root),
        decision_lister=lambda: _owner_decisions(args.repo_root),
        diagnostics_client=_service_diagnostics,
        checkout_revision=lambda: _checkout_revision(args.repo_root),
        main_commit_date=lambda: _main_commit_date(args.repo_root),
        restart_hook=_restart_stale_service,
        daily_budget=args.daily_budget,
    )


def _reconcile_stale_claims(db_path: str) -> None:
    """Release controller claims whose worker stopped heartbeating (#162).

    The API only reconciles at startup, so the timer is where a dead worker is
    noticed. Never resumes work and never fails the tick.
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


def _sweep_stalled_experiments(service) -> None:
    """Fail experiments that logged nothing past their phase limit (#400).

    A hung worker otherwise holds the issue, the daily budget and this tick's
    wait until a restart. Never fails the tick.
    """
    experiment_service = getattr(service, "experiment_service", None)
    if experiment_service is None:
        return
    try:
        stalled = experiment_service.sweep_stalled_experiments()
    except Exception as exc:  # noqa: BLE001 - the sweep must not break a tick
        logger.warning("could not sweep stalled experiments: %s", exc)
        return
    # The notification picks them up through _finished_badly.
    for experiment_id in stalled:
        logger.warning("experiment %s stalled and was marked failed", experiment_id)


def _wait_for_experiments(service=None) -> None:
    """Keep the process alive until the work it started has ended (#358).

    Experiments and adoptions run in daemon threads; the oneshot unit would
    otherwise kill them on exit.
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
            # Wake up now and then: the experiment this tick waits for may be
            # the one whose worker hangs (#400).
            while thread.is_alive():
                thread.join(timeout=_STALL_SWEEP_INTERVAL_SECONDS)
                if thread.is_alive() and service is not None:
                    _sweep_stalled_experiments(service)


def _shadow_replay(repo_root: str, db_path: str) -> None:
    """One local shadow replay of a merged paid experiment (#637), best effort.

    Runs after this tick's own work has ended, only when the local
    llama-server is reachable and idle; free resources only, it never touches
    main, a branch or GitHub. A failure never fails the tick.
    """
    from modules import local_shadow_replay

    try:
        outcome = local_shadow_replay.replay_next(repo_root=repo_root, db_path=db_path)
    except Exception as exc:  # noqa: BLE001 - the replay never owns the tick
        logger.warning("local shadow replay failed: %s", exc)
        return
    if outcome["ran"]:
        row = outcome["replay"] or {}
        logger.info(
            "local shadow replay of %s (%s): %s, %s/%s tests passed",
            outcome["experiment_id"], row.get("prompt_variant"), row.get("status"),
            row.get("tests_passed"), row.get("tests_total"),
        )
    else:
        logger.info("no local shadow replay: %s", outcome["reason"])


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


# Nexus stood still for 17 hours after #506 and every tick only said "idle"
# (#521). After this long without starting work, the owner hears about it,
# at most once a day.
_STALL_ALARM_HOURS = 6
_STALL_ALARM_REPEAT_HOURS = 24
_STALL_ALARM = "stall_alarm"
# Decisions that explain a quiet stretch on their own.
_EXPECTED_QUIET = ("busy", "budget_exhausted", "paused")


def _stall_alarm(db_path: str, now: datetime | None = None) -> str | None:
    """A message when Nexus has started no work for a long time, else ``None``.

    Records the alarm in the initiative log so it is sent once a day.
    """
    now = now or datetime.now(timezone.utc)
    try:
        db = sqlite3.connect(db_path, timeout=30)
    except sqlite3.Error:
        return None
    try:
        rows = db.execute(
            "SELECT created_at, decision, detail FROM nexus_initiative_log ORDER BY created_at"
        ).fetchall()
        if not rows:
            return None
        dispatched = [r[0] for r in rows if r[1] == "dispatched"]
        since = dispatched[-1] if dispatched else rows[0][0]
        window = [r for r in rows if r[0] > since and r[1] != _STALL_ALARM]
        quiet_for = now - datetime.fromisoformat(since)
        if quiet_for < timedelta(hours=_STALL_ALARM_HOURS):
            return None
        if window and window[-1][1] in _EXPECTED_QUIET:
            return None
        alarms = [r[0] for r in rows if r[1] == _STALL_ALARM and r[0] > since]
        if alarms and now - datetime.fromisoformat(alarms[-1]) < timedelta(hours=_STALL_ALARM_REPEAT_HOURS):
            return None
        counts: dict[str, int] = {}
        for _created, decision, _detail in window:
            counts[decision] = counts.get(decision, 0) + 1
        seen = ", ".join(f"{d} x{n}" for d, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        examples = list(dict.fromkeys(
            str(detail)[:150] for _c, decision, detail in reversed(window)
            if detail and decision != "idle"
        ))[:3]
        message = (
            f"Nexus has started no work for {int(quiet_for.total_seconds() // 3600)}h "
            f"(decisions since: {seen or 'none'})"
            + (f"; e.g. {' | '.join(examples)}" if examples else "")
        )
        with db:
            db.execute(
                "INSERT INTO nexus_initiative_log (tick_id, created_at, decision, detail) "
                "VALUES (?, ?, ?, ?)",
                (f"stall-{now.isoformat()}", now.isoformat(), _STALL_ALARM, message[:1000]),
            )
        return message
    except (sqlite3.Error, ValueError, TypeError) as exc:
        logger.warning("stall alarm: %s", exc)
        return None
    finally:
        db.close()


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
    if not args.dry_run:
        # Before the busy check, so a hung experiment does not block this tick.
        _sweep_stalled_experiments(service)
        from modules import nexus_status_issue

        nexus_status_issue.update_for(service, args.repo_root)
    try:
        if not args.dry_run and service.controller is not None:
            resume = service.resume_blocked_capability()
            if resume.decision == "dispatched":
                logger.info(
                    "resumed blocked controller session %s",
                    resume.candidate.ref if resume.candidate else "?",
                )
                return 0
            # budget_exhausted goes on: tick() still adopts verified work (#538).
            if resume.decision in {"busy", "error"}:
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
        _wait_for_experiments(None if args.dry_run else service)
        if not args.dry_run:
            _cleanup_expired_worktrees(service)
            if not args.integration_only:
                _shadow_replay(args.repo_root, args.db_path)
        service.close()
        if not args.dry_run:
            if decision is not None and decision.decision == "error":
                _events.append(f"Tick error: {str(decision.detail or '')[:300]}")
            stalled = _stall_alarm(args.db_path)
            _notify(
                args.repo_root,
                _events + _finished_badly(args.db_path, started_at) + ([stalled] if stalled else []),
            )


if __name__ == "__main__":
    sys.exit(main())
