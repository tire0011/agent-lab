"""Try a local patch on the reviewed branch before starting a new experiment."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Timer

from modules import timing_check
from modules.experiment_adoption_parts.core import STATUS_ADOPTED, STATUS_FAILED, STATUS_READY_FOR_REVIEW
from modules.experiment_evaluation import ChangedFiles, evaluate_implementation
from modules.experiment_handover import HANDOVER_HEADING
from modules.experiment_service import _DEFAULT_TEST_COMMAND_KEY, _TEST_COMMANDS, _utcnow_iso
from modules.github_work_tracking import MergeForbiddenError
from modules.llama_cpp_client import LlamaCppClient
from modules.local_coding_worker import LocalCodingWorker
from modules.local_file_localizer import hint_line
from modules.mailbox import MailboxLock
from modules.nexus_reviewer import review_comment
from modules.redaction import redact_secrets, sanitized_reason
from modules.test_run_log import global_heartbeat_bridge
from modules.verification_runner import run_verification_test_monitored

logger = logging.getLogger(__name__)

_BLOCK = re.compile(r"^Review by (\w+) blocked the merge: (.+)", re.DOTALL)
_NUMBER = re.compile(r"(?:^|\s)\((\d+)\)\s+")
_PATH = re.compile(r"(?<![\w./-])(?:\./)?(?:[\w.-]+/)*[\w.-]+\.[\w-]+(?![\w/-])")
_PATH_TOKEN = re.compile(r"[\w./-]+")
_QUOTED_PATH = re.compile(r"`([^`\n]+)`")
_HUNK = re.compile(r"^@@ ", re.MULTILINE)
_LOCAL_BUDGET_SECONDS = 900


def enabled() -> bool:
    return os.getenv("NEXUS_LOCAL_REVIEW_FIX", "1").strip().lower() not in {
        "0", "false", "off", "no",
    }


def _round_limit() -> int:
    try:
        return max(1, int(os.getenv("NEXUS_LOCAL_REVIEW_FIX_ROUNDS", "3")))
    except ValueError:
        return 3


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "--literal-pathspecs", *args], cwd=cwd, text=True,
        capture_output=True, timeout=120,
    )
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def _findings(summary: str, tracked_paths: set[str]) -> list[tuple[int, str, set[str]]]:
    _, _, listed = summary.partition("Blocking problems: ")
    markers = list(_NUMBER.finditer(listed or summary))
    findings = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else None
        finding = (listed or summary)[marker.end():end].strip()
        paths = {path.removeprefix("./") for path in _PATH.findall(finding)}
        paths.update(
            path.removeprefix("./").rstrip(".")
            for path in _PATH_TOKEN.findall(finding)
            if path.removeprefix("./").rstrip(".") in tracked_paths
        )
        paths.update(path for path in _QUOTED_PATH.findall(finding) if path in tracked_paths)
        findings.append((int(marker[1]), finding, paths))
    return findings


def _timing(tree: Path, diff: str, python: str, remaining: float) -> str:
    timer = None
    tails: dict[str, str] = {}

    def observe(process: subprocess.Popen | None) -> None:
        nonlocal timer
        if process is not None:
            timer = Timer(remaining, timing_check._stop, args=(process,))
            timer.daemon = True
            timer.start()
        elif timer is not None:
            timer.cancel()

    tests = timing_check.affected_tests(diff, tree)
    result = timing_check.run_timing_check(tree, tests, python=python, on_process=observe, tails=tails)
    return "" if result == "ok" else f"Timing check: {result}\n" + "\n".join(tails.values())


@dataclass(slots=True)
class _RepairProgress:
    rounds: int = 0
    tests_passed: bool = False
    covered: list[int] = field(default_factory=list)
    patch: str = ""


def _review_fix_task(service, experiment, goal, summary, paths, branch_diff):
    """The local review-fix coding task plus the matching lessons section (#715).

    The review summary and the touched paths are the match context; the
    returned lesson ids are marked sent by the caller right before the worker
    runs. Returns ``(task, lesson_ids)``.
    """
    task = f"{goal}\n{hint_line(paths)}\n\n{HANDOVER_HEADING}\nBranch diff:\n{branch_diff}"
    return service._with_lessons_section(
        task, experiment, extra_files=paths, extra_text=summary
    )


def _repair(adoption, tree, head, base, summary, experiment_id, elog, remaining, progress):
    tracked_paths = set(_git(tree, "ls-files", "-z").rstrip("\0").split("\0"))
    findings = _findings(summary, tracked_paths)
    if not findings or any(not paths for _, _, paths in findings):
        raise RuntimeError("the numbered findings must each name a file")
    paths = sorted({path for _, _, names in findings for path in names})
    branch_diff = _git(tree, "diff", "--no-ext-diff", "--no-textconv", base, head)
    goal = "Fix every blocking review finding.\n" + "\n".join(
        f"({number}) {text}" for number, text, _ in findings
    )
    task = f"{goal}\n{hint_line(paths)}\n\n{HANDOVER_HEADING}\nBranch diff:\n{branch_diff}"
    service = adoption.experiment_service
    # #715: the local review-fix coding call gets the same matching lessons
    # section as every other coding run; the ids are marked sent right before
    # worker.run. Best-effort: a service without the lessons hooks (or a store
    # error) just runs without the section.
    review_fix_lesson_ids: list[str] = []
    with_lessons = getattr(service, "_with_lessons_section", None)
    if callable(with_lessons):
        try:
            experiment = service.get_experiment(experiment_id)
            task, review_fix_lesson_ids = _review_fix_task(
                service, experiment, goal, summary, paths, branch_diff
            )
        except Exception:  # noqa: BLE001 - lessons never block the repair
            logger.warning("local review fix: lessons section failed", exc_info=True)
    template = service.test_command or _TEST_COMMANDS[_DEFAULT_TEST_COMMAND_KEY]
    python = service.test_python_executable
    command = [part.format(python=python) for part in template]
    failure = ""
    worker = LocalCodingWorker(
        client=LlamaCppClient(timeout=min(300, remaining())), patch_rounds=1,
        likely_files=tuple(paths),
    )
    for round_no in range(1, _round_limit() + 1):
        budget = remaining()
        worker.client.timeout = min(300, budget)
        feedback = (
            f"\n\nPrevious local round failure:\n{failure}\n"
            f"Local patch so far:\n{progress.patch[:8000]}" if failure else ""
        )
        progress.rounds = round_no
        mark_sent = getattr(service, "_mark_lessons_sent", None)
        if callable(mark_sent):
            try:
                mark_sent(experiment_id, review_fix_lesson_ids)
            except Exception:  # noqa: BLE001 - bookkeeping only
                logger.warning("local review fix: could not track lesson injection", exc_info=True)
        result = worker.run(
            task=task + feedback, working_dir=str(tree), sandbox="workspace-write",
            timeout_seconds=budget, experiment_logger=elog, phase="local_review_fix",
        )
        if not result.success:
            failure = result.error or result.stderr or "Local worker did not produce a patch"
            if not progress.patch:
                progress.patch = worker.last_attempted_patch
            samples = [sample for stats in worker.last_sample_stats for sample in stats.samples]
            if not samples or samples[-1]["outcome"] in {"model_error", "model_timeout", "rollback_failed"}:
                break
            continue
        progress.tests_passed = False
        _git(tree, "add", "-A")
        progress.patch = _git(tree, "diff", "--cached", "--no-ext-diff", "--no-renames", head)
        candidate = _git(tree, "write-tree").strip()
        changed = _git(tree, "diff", "--name-only", "-z", "--no-renames", base, candidate)
        changed_paths = changed.rstrip("\0").split("\0") if changed else []
        denied = next((path for path in changed_paths if adoption._denied_path_reason(path)), None)
        if denied is not None:
            failure = f"Local fix touched a protected path: {denied}"
            break
        _git(tree, "diff", "--check", head, candidate)
        hunks = {
            path for path in paths if _HUNK.search(_git(
                tree, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", head, candidate, "--", path,
            ))
        }
        progress.covered = [number for number, _, names in findings if names & hunks]
        watchdog = service._get_test_watchdog_config()
        test_run = run_verification_test_monitored(
            command, cwd=str(tree), run_id=f"local-review-fix-{experiment_id}-{round_no}",
            config=replace(watchdog, safety_cap_seconds=min(watchdog.safety_cap_seconds, remaining())),
            on_heartbeat=global_heartbeat_bridge(elog, "local_review_fix"),
        )
        if test_run.exit_code != 0 or test_run.timed_out or test_run.exception:
            _git(tree, "read-tree", "--reset", "-u", candidate)
            _git(tree, "clean", "-fd")
            failure = f"Verification tests failed (exit {test_run.exit_code})\n{test_run.output[-4000:]}"
            continue
        diff = _git(tree, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", base, candidate)
        failure = _timing(tree, diff, python, remaining())[-4000:]
        progress.tests_passed = not failure
        if _git(tree, "diff", "--name-only") or _git(tree, "write-tree").strip() != candidate:
            raise RuntimeError("verification modified the candidate tree")
        _git(tree, "clean", "-fd")
        if failure:
            continue
        if len(progress.covered) != len(findings):
            missing = [number for number, _, _ in findings if number not in progress.covered]
            failure = f"No changed hunk for findings {missing}"
            continue
        return candidate, changed_paths
    raise RuntimeError(failure)


def try_local_review_fix(adoption, experiment_id: str) -> tuple[bool, str]:
    """Return whether the retry was handled and any diagnostics for the paid coder."""
    record = adoption.get(experiment_id)
    block = _BLOCK.match(record.reason or "")
    if record.status != STATUS_FAILED or not block or adoption.reviewer is None:
        return False, ""

    elog = adoption.experiment_service.get_experiment_logger(experiment_id)
    lock = MailboxLock(adoption.lock_path)
    adoption.lock_path.parent.mkdir(parents=True, exist_ok=True)
    if not lock.acquire():
        # Let the next tick retry rather than running two writers on this issue.
        return True, ""

    started = time.monotonic()
    progress = _RepairProgress()
    review_requested = False
    accepted = False
    local_seconds = None
    outcome = "paid_fallback"
    merging = False
    tracking = adoption.github_work_tracking

    def remaining() -> float:
        seconds = _LOCAL_BUDGET_SECONDS - (time.monotonic() - started)
        if seconds <= 0:
            raise TimeoutError("local review fix ran out of time")
        return seconds

    try:
        current = adoption.get(experiment_id)
        if current.status != STATUS_FAILED or current.reason != record.reason:
            outcome = "superseded"
            return True, ""
        ref = tracking.for_experiment(experiment_id)
        if ref is None or ref.pr_number is None or not record.integrated_commit_sha:
            raise RuntimeError("the blocked pull request and its reviewed tree are required")
        merged = tracking.merged_commit_sha(ref)
        if merged:
            outcome = "already_merged"
        else:
            reviewer, summary = block.groups()
            load_review = getattr(adoption.reviewer, "latest_blocking_review", None)
            original = load_review(experiment_id) if callable(load_review) else None
            if original and original[0] == reviewer:
                # Use the full findings only when they describe this recorded
                # block. A later follow-up can have a different set.
                original_reason = f"Review by {reviewer} blocked the merge: {original[1]}"
                if sanitized_reason(original_reason, max_chars=len(record.reason)) == record.reason:
                    summary = redact_secrets(original[1])
            service = adoption.experiment_service
            experiment = service.get_experiment(experiment_id)
            root = adoption.repo_root
            _git(root, "fetch", "--no-tags", "origin", "main", f"refs/heads/{ref.branch_name}")
            head = tracking._remote_branch_sha(ref.branch_name)
            base = _git(root, "rev-parse", "origin/main").strip()
            if _git(root, "rev-parse", f"{record.integrated_commit_sha}^").strip() != base:
                raise RuntimeError("the blocked branch needs integration with current main")
            if _git(root, "rev-parse", f"{head}^{{tree}}") != _git(
                root, "rev-parse", f"{record.integrated_commit_sha}^{{tree}}"
            ):
                raise RuntimeError("the issue branch no longer matches the reviewed tree")

            with tempfile.TemporaryDirectory(prefix="nexus-local-review-fix-") as temp:
                tree = Path(temp) / "review"
                _git(root, "worktree", "add", "--detach", str(tree), head)
                try:
                    candidate, changed_paths = _repair(
                        adoption, tree, head, base, summary, experiment_id, elog, remaining, progress,
                    )
                    evaluation = evaluate_implementation(
                        experiment.plan, experiment.goal, ChangedFiles(tracked_modified=changed_paths),
                        str(tree),
                    )
                    candidate_findings = evaluation.soft_findings()
                    if evaluation.outcome != "pass" and candidate_findings is None:
                        raise RuntimeError(f"Quality gate: {evaluation.reason}")
                    prior_findings = (getattr(experiment, "quality_evaluation", None) or {}).get("review_findings")
                    soft = "; ".join(dict.fromkeys(f for f in (prior_findings, candidate_findings) if f))
                    _git(root, "fetch", "--no-tags", "origin", "main")
                    if _git(root, "rev-parse", "origin/main").strip() != base:
                        raise RuntimeError("main moved during local verification")
                    remaining()
                    published = tracking.publish_final_tree(
                        ref, integrated_commit_sha=candidate, worktree_path=str(tree),
                        experiment_id=experiment_id, expected_head_sha=head,
                    )
                    _git(tree, "reset", "--hard", published)
                    local_seconds = time.monotonic() - started
                    review_requested = True
                    verdict = adoption.reviewer.review(
                        experiment_id=experiment_id, goal=experiment.goal, worktree_path=str(tree),
                        base_sha=base, earlier_review=(reviewer, summary),
                        gate_findings=soft,
                        model_written_tests=getattr(experiment, "model_written_tests", None) or "",
                    )
                    try:
                        tracking.comment(ref.pr_number, review_comment(verdict))
                    except Exception:
                        logger.warning("Could not post local fix review on PR #%s", ref.pr_number)
                    accepted = verdict.reviewer == reviewer and verdict.verdict == "approve"
                    if not accepted:
                        raise RuntimeError(f"Follow-up review by {verdict.reviewer}: {verdict.summary}")
                    ci = tracking.wait_for_commit_ci(published, experiment_id=experiment_id)
                    if ci.status != "passed":
                        raise RuntimeError(f"CI on the local review fix: {ci.status} {ci.reason or ''}")
                    _git(root, "fetch", "--no-tags", "origin", "main")
                    if _git(root, "rev-parse", "origin/main").strip() != base:
                        raise RuntimeError("main moved during local re-review")
                    merging = True
                    try:
                        merged = tracking.merge_and_complete(ref, expected_head_sha=published)
                    except MergeForbiddenError as exc:
                        adoption._set_status(
                            experiment_id, STATUS_READY_FOR_REVIEW, reason=redact_secrets(str(exc)),
                            integrated_commit_sha=published, updated_at=_utcnow_iso(),
                            ended_at=_utcnow_iso(),
                        )
                        outcome = "ready_for_review"
                        return True, ""
                    outcome = "merged"
                finally:
                    try:
                        _git(root, "worktree", "remove", "--force", str(tree))
                    except Exception:
                        logger.warning("Could not remove local review worktree %s", tree)

        adoption._set_status(
            experiment_id, STATUS_ADOPTED, ended_at=_utcnow_iso(), integrated_commit_sha=merged,
            reason="Local review fix merged", local_sync_pending=1, updated_at=_utcnow_iso(),
        )
        try:
            _git(adoption.repo_root, "fetch", "origin", "main")
            pending, reason = adoption._sync_local_repo(elog)
            adoption._set_status(
                experiment_id, STATUS_ADOPTED, local_sync_pending=int(pending),
                reason=reason, updated_at=_utcnow_iso(),
            )
        except Exception:
            logger.warning("Local checkout synchronization remains pending for %s", experiment_id)
        adoption._report_issue_progress(experiment_id, "completed", f"Merged into main as {merged[:12]}.")
        return True, ""
    except Exception as exc:
        if merging:
            # A failed response can follow a successful merge. Never start a
            # paid retry until the next tick can reconcile the pull request.
            outcome = "merge_pending"
            raise
        failure = redact_secrets(str(exc))[-4000:]
        return False, (
            "\n\nLocal review fix diagnostics\n"
            f"Failure tail:\n{failure}\n"
            f"Local diff (unverified):\n{redact_secrets(progress.patch)[:8000]}"
        )
    finally:
        lock.release()
        elog.info(
            "adoption", "local_review_fix", f"Local review fix {outcome}", rounds=progress.rounds,
            seconds=round(local_seconds if local_seconds is not None else time.monotonic() - started, 3),
            tests_passed=progress.tests_passed,
            findings_covered=progress.covered, outcome=outcome, review_requested=review_requested,
            reviewer_accepted=accepted,
            paid_coder_avoided=outcome in {"merged", "ready_for_review"},
        )
