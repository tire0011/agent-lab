"""Supervise parked-branch maintenance from the API service's background thread."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from modules.verification_runner import _kill_process_tree

from . import ExperimentService
from .git_utils import _run_git
from .reverify import _issue_number, _now, _record, current_main, parked_candidates, reverify_parked

logger = logging.getLogger(__name__)

_TIMEOUT = "NEXUS_REVERIFY_TIMEOUT_SECONDS"
_DEFAULT_TIMEOUT = 45 * 60


def _seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if 0 < value < float("inf") else default


def _supervise_attempt(service: ExperimentService, db_path: str, experiment_id: str,
                       main_sha: str, *, timeout: float) -> str | None:
    started = _now()
    deadline = time.monotonic() + timeout
    experiment = service.get_experiment(experiment_id)
    issue = _issue_number(service, experiment)
    root = Path(tempfile.mkdtemp(prefix="nexus-reverify-"))
    verdict = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "modules.experiment_service.reverify_worker",
             db_path, str(service.repo_root),
             experiment_id, main_sha, str(root)],
            env={**os.environ, "TMPDIR": str(root)},
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _kill_process_tree(process.pid)
            # Avoid delaying the durable verdict if the kernel cannot yet reap
            # a task blocked in I/O after SIGKILL.
            verdict = "reverify_timeout"
            _record(service, experiment_id, issue, main_sha, experiment.branch_name,
                    None, None, verdict, f"wall-clock limit of {timeout:g}s", started)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("re-verification process %s has not exited after SIGKILL", process.pid)
        else:
            if process.returncode != 0:
                logger.warning("re-verification of %s exited %s", experiment_id, process.returncode)
            else:
                verdict = _publish_outcome(service, root)
        return verdict
    finally:
        if verdict != "passes_now":
            try:
                for worktree in (root / "worktree", *root.glob("nexus-baseline-*/worktree")):
                    try:
                        _run_git(["worktree", "remove", "--force", str(worktree)],
                                 cwd=str(service.repo_root), timeout=15)
                    except (OSError, subprocess.TimeoutExpired):
                        logger.warning("could not remove re-verification worktree %s", worktree)
            finally:
                shutil.rmtree(root, ignore_errors=True)


def _publish_outcome(service: ExperimentService, root: Path) -> str | None:
    try:
        outcome = json.loads((root / "outcome.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return _record(service, **outcome)["verdict"]


def run_parked_maintenance(db_path: str, repo_root: str) -> None:
    """Run at most one actual attempt per hand-off, across API workers."""
    with open(f"{db_path}.reverify.lock", "a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        service = ExperimentService(db_path=db_path, repo_root=repo_root)
        try:
            # Avoid fetching on every tick when there is no parked work.
            if not parked_candidates(service, "", limit=1):
                return
            main_sha = current_main(repo_root)
            timeout = _seconds(_TIMEOUT, _DEFAULT_TIMEOUT)
            for experiment_id in parked_candidates(service, main_sha, limit=20):
                verdict = _supervise_attempt(service, db_path, experiment_id, main_sha, timeout=timeout)
                if not (verdict or "").startswith("skipped:"):
                    break
        finally:
            service.close()


def _issue_is_open(repo_root: str, issue: int) -> bool:
    result = subprocess.run(
        ["gh", "issue", "view", str(issue), "--json", "state"],
        cwd=repo_root, capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(result.stdout)["state"] == "OPEN"


def _run_attempt(db_path: str, repo_root: str, experiment_id: str,
                 main_sha: str, root: Path) -> None:
    stall = _seconds("NEXUS_REVERIFY_STALL_SECONDS", 10 * 60)
    service = ExperimentService(
        db_path=db_path, repo_root=repo_root, test_stall_timeout_seconds=stall,
        test_watchdog_interval_seconds=min(5, stall),
        test_safety_cap_seconds=_seconds(_TIMEOUT, _DEFAULT_TIMEOUT),
    )
    try:
        # The normal verifier writes intermediate experiment status and PGIDs.
        # Shadow that row on this connection so the tick sees parked work and a
        # hard kill cannot leave an active experiment or a stale worktree path.
        with service._lock, service._conn:
            service._conn.execute(
                "CREATE TEMP TABLE experiments AS SELECT * FROM main.experiments WHERE id=?",
                (experiment_id,),
            )
        reverify_parked(
            experiment_id, service=service, main_sha=main_sha, worktree_root=root,
            issue_is_open=lambda issue: _issue_is_open(repo_root, issue),
            outcome_path=root / "outcome.json",
        )
    finally:
        service.close()


if __name__ == "__main__":
    db, repo, experiment, main, root = sys.argv[1:]
    _run_attempt(db, repo, experiment, main, Path(root))
