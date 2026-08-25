from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modules.experiment_evaluation import ChangedFiles, evaluate_implementation, parse_git_status_short
from modules.experiment_service import (
    STATUS_SUCCEEDED,
    _ALLOWED_TEST_COMMANDS,
    _DEFAULT_TEST_COMMAND_KEY,
    _run_git,
    _truncate_text,
    _utcnow_iso,
)
from modules.mailbox import MailboxLock
from modules.redaction import bounded_lines, redact, sanitized_reason
from modules.run_store import DEFAULT_DB_PATH, init_db
from modules.verification_runner import run_verification_test

logger = logging.getLogger(__name__)

STATUS_NOT_STARTED = "not_started"
STATUS_QUEUED = "queued"
STATUS_ADOPTING = "adopting"
STATUS_ADOPTED = "adopted"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"

_TERMINAL_STATUSES = {STATUS_ADOPTED, STATUS_NEEDS_REVIEW, STATUS_FAILED, STATUS_INTERRUPTED}
_ACTIVE_STATUSES = {STATUS_QUEUED, STATUS_ADOPTING}

_DEFAULT_TEST_TIMEOUT_SECONDS = 300
_DEFAULT_GIT_TIMEOUT_SECONDS = 60
_DEFAULT_TARGET_BRANCH = "main"
_DEFAULT_LOCK_PATH = "/var/lib/nexus/adoption.lock"


class ExperimentAdoptionError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class ExperimentAdoption:
    id: str | None
    experiment_id: str
    status: str
    started_at: str | None = None
    ended_at: str | None = None
    experiment_commit_sha: str | None = None
    integrated_commit_sha: str | None = None
    target_branch: str = _DEFAULT_TARGET_BRANCH
    expected_files: list[str] | None = None
    actual_files: list[str] | None = None
    test_exit_code: int | None = None
    test_output: str | None = None
    reason: str | None = None
    local_sync_pending: bool = False
    diagnostic_worktree_path: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "experiment_id": self.experiment_id,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "experiment_commit_sha": self.experiment_commit_sha,
            "integrated_commit_sha": self.integrated_commit_sha,
            "target_branch": self.target_branch,
            "expected_files": list(self.expected_files or []),
            "actual_files": list(self.actual_files or []),
            "test_exit_code": self.test_exit_code,
            "test_output_lines": bounded_lines(self.test_output, max_lines=200),
            "reason": self.reason,
            "local_sync_pending": self.local_sync_pending,
            "diagnostic_worktree_path": self.diagnostic_worktree_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _json_or_none(value: str | None) -> list[str] | None:
    if not value:
        return None
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, list):
        return None
    return [str(item) for item in loaded]


def _row_to_adoption(row: sqlite3.Row | None, experiment_id: str) -> ExperimentAdoption:
    if row is None:
        return ExperimentAdoption(
            id=None,
            experiment_id=experiment_id,
            status=STATUS_NOT_STARTED,
            target_branch=_DEFAULT_TARGET_BRANCH,
        )
    return ExperimentAdoption(
        id=row["id"],
        experiment_id=row["experiment_id"],
        status=row["status"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        experiment_commit_sha=row["experiment_commit_sha"],
        integrated_commit_sha=row["integrated_commit_sha"],
        target_branch=row["target_branch"] or _DEFAULT_TARGET_BRANCH,
        expected_files=_json_or_none(row["expected_files_json"]),
        actual_files=_json_or_none(row["actual_files_json"]),
        test_exit_code=row["test_exit_code"],
        test_output=row["test_output"],
        reason=row["reason"],
        local_sync_pending=bool(row["local_sync_pending"]),
        diagnostic_worktree_path=row["diagnostic_worktree_path"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _normalize_path(path: str) -> str:
    normalized = (path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _collapse_summary(raw: str | None, max_chars: int = 120) -> str:
    if not raw:
        return "experiment"
    text = " ".join(redact(raw).split())
    return text[:max_chars] if text else "experiment"


def _safe_list(values: list[str] | None, *, limit: int = 100) -> list[str]:
    result: list[str] = []
    for value in values or []:
        item = _normalize_path(str(value))[:200]
        if item and item not in result:
            result.append(item)
    return result[:limit]


class ExperimentAdoptionService:
    """Persisted, exactly-once experiment adoption state machine."""

    def __init__(
        self,
        *,
        experiment_service,
        db_path: str = DEFAULT_DB_PATH,
        repo_root: str = "/agent",
        worktrees_root: str | None = None,
        lock_path: str = _DEFAULT_LOCK_PATH,
        test_timeout_seconds: int = _DEFAULT_TEST_TIMEOUT_SECONDS,
    ) -> None:
        self.experiment_service = experiment_service
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self.worktrees_root = Path(worktrees_root or getattr(experiment_service, "worktrees_root", str(Path.home() / "agent-worktrees")))
        self.lock_path = Path(lock_path)
        self.test_timeout_seconds = test_timeout_seconds
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        conn = init_db(db_path)
        conn.row_factory = sqlite3.Row
        self._conn = conn

    def close(self) -> None:
        self._conn.close()

    def get(self, experiment_id: str) -> ExperimentAdoption:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM experiment_adoptions WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        return _row_to_adoption(row, experiment_id)

    def wait_for_thread(self, experiment_id: str, timeout: float | None = None) -> None:
        thread = self._threads.get(experiment_id)
        if thread is not None:
            thread.join(timeout)

    def recover_after_restart(self) -> list[str]:
        ended_at = _utcnow_iso()
        reason = (
            "interrupted: adoption was still queued/adopting at service startup "
            "(process restart or crash); no automatic recovery is implemented"
        )
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT experiment_id FROM experiment_adoptions WHERE status IN (?, ?)",
                (STATUS_QUEUED, STATUS_ADOPTING),
            ).fetchall()
            ids = [row["experiment_id"] for row in rows]
            self._conn.executemany(
                """
                UPDATE experiment_adoptions
                SET status = ?, ended_at = ?, reason = ?, updated_at = ?
                WHERE experiment_id = ? AND status IN (?, ?)
                """,
                [
                    (
                        STATUS_INTERRUPTED,
                        ended_at,
                        sanitized_reason(reason),
                        ended_at,
                        experiment_id,
                        STATUS_QUEUED,
                        STATUS_ADOPTING,
                    )
                    for experiment_id in ids
                ],
            )
        return ids

    def start_adoption(self, experiment_id: str) -> ExperimentAdoption:
        existing = self.get(experiment_id)
        if existing.status in {STATUS_ADOPTED, STATUS_QUEUED, STATUS_ADOPTING, STATUS_NEEDS_REVIEW}:
            return existing

        other = self._find_active_other(experiment_id)
        if other is not None:
            raise ExperimentAdoptionError("Eine andere Übernahme läuft bereits.")
        if not self._lock_probe():
            raise ExperimentAdoptionError("Eine andere Übernahme läuft bereits.")

        validation = self._validate_startable(experiment_id, persist_review=True)
        now = _utcnow_iso()
        adoption_id = existing.id or uuid.uuid4().hex
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO experiment_adoptions (
                    id, experiment_id, status, started_at, ended_at,
                    experiment_commit_sha, integrated_commit_sha, target_branch,
                    expected_files_json, actual_files_json, test_exit_code,
                    test_output, reason, local_sync_pending,
                    diagnostic_worktree_path, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, NULL, NULL, NULL, 0, NULL, ?, ?)
                ON CONFLICT(experiment_id) DO UPDATE SET
                    status=excluded.status,
                    started_at=NULL,
                    ended_at=NULL,
                    experiment_commit_sha=NULL,
                    integrated_commit_sha=NULL,
                    target_branch=excluded.target_branch,
                    expected_files_json=excluded.expected_files_json,
                    actual_files_json=excluded.actual_files_json,
                    test_exit_code=NULL,
                    test_output=NULL,
                    reason=NULL,
                    local_sync_pending=0,
                    diagnostic_worktree_path=NULL,
                    updated_at=excluded.updated_at
                """,
                (
                    adoption_id,
                    experiment_id,
                    STATUS_QUEUED,
                    _DEFAULT_TARGET_BRANCH,
                    json.dumps(validation["expected_files"]),
                    json.dumps(validation["actual_files"]),
                    now,
                    now,
                ),
            )
        thread = threading.Thread(
            target=self._run_thread,
            args=(adoption_id, experiment_id),
            daemon=True,
            name=f"adopt-{experiment_id[:8]}",
        )
        self._threads[experiment_id] = thread
        thread.start()
        return self.get(experiment_id)

    def _run_thread(self, adoption_id: str, experiment_id: str) -> None:
        try:
            self._run_adoption_pipeline(adoption_id, experiment_id)
        except Exception:  # noqa: BLE001
            logger.exception("experiment adoption %s failed unexpectedly", experiment_id)
            try:
                self._set_status(
                    experiment_id,
                    STATUS_FAILED,
                    ended_at=_utcnow_iso(),
                    reason=sanitized_reason("unexpected adoption failure"),
                )
            except Exception:
                logger.exception("failed to persist unexpected adoption error for %s", experiment_id)

    def _find_active_other(self, experiment_id: str) -> ExperimentAdoption | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM experiment_adoptions WHERE experiment_id != ? AND status IN (?, ?) LIMIT 1",
                (experiment_id, STATUS_QUEUED, STATUS_ADOPTING),
            ).fetchone()
        return _row_to_adoption(row, row["experiment_id"]) if row is not None else None

    def _lock_probe(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = MailboxLock(self.lock_path)
        try:
            return lock.acquire()
        finally:
            lock.release()

    def _update_review_state(self, experiment_id: str, reason: str, *, actual_files: list[str], expected_files: list[str]) -> ExperimentAdoption:
        now = _utcnow_iso()
        adoption_id = self.get(experiment_id).id or uuid.uuid4().hex
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO experiment_adoptions (
                    id, experiment_id, status, started_at, ended_at,
                    experiment_commit_sha, integrated_commit_sha, target_branch,
                    expected_files_json, actual_files_json, test_exit_code,
                    test_output, reason, local_sync_pending,
                    diagnostic_worktree_path, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, ?, NULL, NULL, ?, ?, ?, NULL, NULL, ?, 0, NULL, ?, ?)
                ON CONFLICT(experiment_id) DO UPDATE SET
                    status=excluded.status,
                    ended_at=excluded.ended_at,
                    expected_files_json=excluded.expected_files_json,
                    actual_files_json=excluded.actual_files_json,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                (
                    adoption_id,
                    experiment_id,
                    STATUS_NEEDS_REVIEW,
                    now,
                    _DEFAULT_TARGET_BRANCH,
                    json.dumps(_safe_list(expected_files)),
                    json.dumps(_safe_list(actual_files)),
                    sanitized_reason(reason),
                    now,
                    now,
                ),
            )
        return self.get(experiment_id)

    def _validate_startable(self, experiment_id: str, *, persist_review: bool) -> dict[str, Any]:
        experiment = self.experiment_service.get_experiment(experiment_id)
        if experiment.status != STATUS_SUCCEEDED:
            raise ExperimentAdoptionError("Nur erfolgreiche Experimente können übernommen werden.")
        quality = experiment.quality_evaluation or {}
        if quality.get("outcome") != "pass":
            raise ExperimentAdoptionError("Das Qualitäts-Gate des Experiments ist nicht bestanden.")
        worktree_path = experiment.worktree_path or ""
        if not worktree_path or not os.path.isdir(worktree_path):
            raise ExperimentAdoptionError("Der Experiment-Worktree ist nicht mehr verfügbar.")

        elog = self.experiment_service.get_experiment_logger(experiment_id)
        snapshot = self._capture_snapshot(worktree_path, elog, phase="adoption")
        current_status = (snapshot["git_status"] or "").strip()
        stored_status = (experiment.git_status or "").strip()
        current_stat = (snapshot["diff_stat"] or "").strip()
        stored_stat = (experiment.diff_stat or "").strip()
        if current_status != stored_status or current_stat != stored_stat:
            raise ExperimentAdoptionError(
                "Die Änderungen im Experiment-Worktree haben sich seit der erfolgreichen Prüfung verändert."
            )

        actual_files = _safe_list(snapshot["changed"].all_paths())
        expected_files = _safe_list((quality.get("changed_files") or quality.get("expected_files") or actual_files))
        review_reason = self._review_reason(actual_files, expected_files)
        if review_reason is not None:
            if persist_review:
                self._update_review_state(
                    experiment_id,
                    review_reason,
                    actual_files=actual_files,
                    expected_files=expected_files,
                )
            raise ExperimentAdoptionError(review_reason)

        return {
            "experiment": experiment,
            "actual_files": actual_files,
            "expected_files": expected_files,
            "snapshot": snapshot,
        }

    def _review_reason(self, actual_files: list[str], expected_files: list[str]) -> str | None:
        for path in actual_files:
            reason = self._denied_path_reason(path)
            if reason is not None:
                return reason
        if expected_files:
            extras = [path for path in actual_files if path not in expected_files]
            if extras:
                return "Unerwartete Dateien im Experiment-Diff; manuelle Prüfung nötig."
        return None

    def _denied_path_reason(self, path: str) -> str | None:
        normalized = _normalize_path(path)
        lower = normalized.lower()
        if not normalized:
            return "Leere Dateipfade im Experiment-Diff sind unzulässig."
        parts = Path(normalized).parts
        if normalized.startswith("/") or ".." in parts:
            return "Dateien außerhalb des Repositories erfordern manuelle Prüfung."
        if lower.startswith(".git/") or "/.git/" in lower:
            return "Änderungen an Git-Metadaten erfordern manuelle Prüfung."
        if lower.startswith("communication/outbox/"):
            return "Änderungen unter communication/outbox/ werden nicht automatisch übernommen."
        if lower == ".env" or lower.endswith("/.env") or lower.endswith(".pem"):
            return "Sensible Konfigurationsdateien werden nicht automatisch übernommen."
        if "secret" in lower or "credential" in lower:
            return "Potentiell sensible Dateien werden nicht automatisch übernommen."
        return None

    def _capture_snapshot(self, worktree_path: str, elog, *, phase: str) -> dict[str, Any]:
        status_proc = _run_git(
            ["status", "--short", "--untracked-files=all"],
            cwd=worktree_path,
            timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
            elog=elog,
            phase=phase,
        )
        scratch_dir = self.worktrees_root / f"adoption-index-{uuid.uuid4().hex}"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        index_env = dict(os.environ)
        index_env["GIT_INDEX_FILE"] = str(scratch_dir / "index")
        try:
            read_tree = _run_git(
                ["read-tree", "HEAD"],
                cwd=worktree_path,
                env=index_env,
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase=phase,
            )
            add_proc = _run_git(
                ["add", "-A"],
                cwd=worktree_path,
                env=index_env,
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase=phase,
            )
            if read_tree.returncode != 0 or add_proc.returncode != 0:
                failed = read_tree if read_tree.returncode != 0 else add_proc
                raise subprocess.CalledProcessError(failed.returncode, failed.args, output=failed.stdout, stderr=failed.stderr)
            stat_proc = _run_git(
                ["diff", "--cached", "--stat", "HEAD"],
                cwd=worktree_path,
                env=index_env,
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase=phase,
            )
            diff_proc = _run_git(
                ["diff", "--cached", "HEAD"],
                cwd=worktree_path,
                env=index_env,
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase=phase,
            )
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)
        return {
            "git_status": status_proc.stdout,
            "diff_stat": stat_proc.stdout,
            "diff_text": diff_proc.stdout,
            "changed": parse_git_status_short(status_proc.stdout),
        }

    def _transition_status(self, experiment_id: str, from_status: str, to_status: str, **fields: Any) -> bool:
        set_clauses = ["status = ?"]
        values: list[Any] = [to_status]
        for key, value in fields.items():
            set_clauses.append(f"{key} = ?")
            values.append(value)
        values.extend([experiment_id, from_status])
        query = f"UPDATE experiment_adoptions SET {', '.join(set_clauses)} WHERE experiment_id = ? AND status = ?"
        with self._lock, self._conn:
            updated = self._conn.execute(query, values).rowcount
        return bool(updated)

    def _set_status(self, experiment_id: str, status: str, **fields: Any) -> None:
        set_clauses = ["status = ?"]
        values: list[Any] = [status]
        for key, value in fields.items():
            set_clauses.append(f"{key} = ?")
            values.append(value)
        values.append(experiment_id)
        query = f"UPDATE experiment_adoptions SET {', '.join(set_clauses)} WHERE experiment_id = ?"
        with self._lock, self._conn:
            self._conn.execute(query, values)

    def _run_adoption_pipeline(self, adoption_id: str, experiment_id: str) -> ExperimentAdoption:
        elog = self.experiment_service.get_experiment_logger(experiment_id)
        lock = MailboxLock(self.lock_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if not lock.acquire():
            self._set_status(
                experiment_id,
                STATUS_FAILED,
                ended_at=_utcnow_iso(),
                reason=sanitized_reason("Eine andere Übernahme läuft bereits."),
                updated_at=_utcnow_iso(),
            )
            return self.get(experiment_id)

        integration_path: Path | None = None
        keep_integration = False
        try:
            if not self._transition_status(
                experiment_id,
                STATUS_QUEUED,
                STATUS_ADOPTING,
                started_at=_utcnow_iso(),
                updated_at=_utcnow_iso(),
            ):
                return self.get(experiment_id)

            elog.info("adoption", "adoption_started", "Starting persisted experiment adoption")
            fetch_proc = _run_git(
                ["fetch", "origin", _DEFAULT_TARGET_BRANCH],
                cwd=str(self.repo_root),
                timeout=120,
                elog=elog,
                phase="adoption",
            )
            if fetch_proc.returncode != 0:
                return self._finish_failure(
                    experiment_id,
                    reason="git fetch origin main failed",
                    elog=elog,
                )

            validation = self._validate_startable(experiment_id, persist_review=False)
            experiment = validation["experiment"]
            worktree_path = str(experiment.worktree_path)
            snapshot = validation["snapshot"]
            actual_files = validation["actual_files"]
            expected_files = validation["expected_files"]
            diff_text = _truncate_text(snapshot["diff_text"], 64 * 1024, label="diff")
            elog.debug(
                "adoption",
                "adoption_diff_captured",
                "Captured experiment diff prior to commit",
                changed_files=actual_files,
                diff_stat=sanitized_reason(snapshot["diff_stat"], max_chars=1000),
            )
            if not actual_files:
                return self._finish_failure(
                    experiment_id,
                    reason="Keine Änderungen zur Übernahme vorhanden.",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    elog=elog,
                )

            _run_git(["reset", "HEAD", "--"], cwd=worktree_path, timeout=60, elog=elog, phase="adoption")
            for rel_path in actual_files:
                add_proc = _run_git(
                    ["add", "--", rel_path],
                    cwd=worktree_path,
                    timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                    elog=elog,
                    phase="adoption",
                )
                if add_proc.returncode != 0:
                    return self._finish_failure(
                        experiment_id,
                        reason=f"git add failed for {Path(rel_path).name}",
                        actual_files=actual_files,
                        expected_files=expected_files,
                        elog=elog,
                    )

            commit_message = self._commit_message(experiment_id, experiment.goal, experiment.plan)
            commit_proc = _run_git(
                ["commit", "--no-verify", "-m", commit_message],
                cwd=worktree_path,
                timeout=120,
                elog=elog,
                phase="adoption",
            )
            if commit_proc.returncode != 0:
                return self._finish_failure(
                    experiment_id,
                    reason="Der Experiment-Commit konnte nicht erstellt werden.",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    elog=elog,
                )
            experiment_commit_sha = self._git_stdout(["rev-parse", "HEAD"], worktree_path, elog).strip() or None

            integration_path = self.worktrees_root / f"adopt-{experiment_id[:8]}-{uuid.uuid4().hex[:8]}"
            integration_path.parent.mkdir(parents=True, exist_ok=True)
            add_worktree = _run_git(
                ["worktree", "add", str(integration_path), f"origin/{_DEFAULT_TARGET_BRANCH}"],
                cwd=str(self.repo_root),
                timeout=120,
                elog=elog,
                phase="adoption",
            )
            if add_worktree.returncode != 0:
                return self._finish_failure(
                    experiment_id,
                    reason="Der Integrations-Worktree konnte nicht erstellt werden.",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    elog=elog,
                )

            cherry_pick = _run_git(
                ["cherry-pick", "--no-commit", experiment_commit_sha or "HEAD"],
                cwd=str(integration_path),
                timeout=120,
                elog=elog,
                phase="adoption",
            )
            if cherry_pick.returncode != 0:
                keep_integration = True
                _run_git(
                    ["cherry-pick", "--abort"],
                    cwd=str(integration_path),
                    timeout=60,
                    elog=elog,
                    phase="adoption",
                )
                return self._finish_review(
                    experiment_id,
                    reason="Integrationskonflikt beim Cherry-Pick.",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )

            test_command_template = _ALLOWED_TEST_COMMANDS[_DEFAULT_TEST_COMMAND_KEY]
            test_command = [part.format(python=self.experiment_service.test_python_executable) for part in test_command_template]
            test_timeout_seconds = self.experiment_service._get_test_timeout_seconds()
            test_run = run_verification_test(
                test_command,
                cwd=str(integration_path),
                timeout_seconds=test_timeout_seconds,
            )
            test_exit_code = test_run.exit_code
            test_output = test_run.output
            if test_run.timed_out:
                keep_integration = True
                elog.log_process(
                    "adoption", "integration_tests_timeout",
                    "Integration test command timed out",
                    command=test_command, cwd=str(integration_path),
                    duration_ms=test_run.duration_ms,
                    timed_out=True, level="ERROR",
                    timeout_seconds=test_timeout_seconds,
                )
                next_timeout_seconds = self.experiment_service._increase_test_timeout_seconds(test_timeout_seconds)
                if next_timeout_seconds > test_timeout_seconds:
                    elog.warning(
                        "adoption", "test_timeout_increased",
                        f"Verification test timeout increased from {test_timeout_seconds}s to "
                        f"{next_timeout_seconds}s for future runs",
                        previous_timeout_seconds=test_timeout_seconds,
                        new_timeout_seconds=next_timeout_seconds,
                    )
            elif test_run.exception is not None:
                keep_integration = True
                elog.error(
                    "adoption", "integration_tests_exception",
                    "Integration test command raised an exception",
                    exc=test_run.exception, command=test_command,
                    cwd=str(integration_path),
                )
            else:
                elog.log_process(
                    "adoption", "integration_tests_completed",
                    f"Integration tests finished with exit code {test_exit_code}",
                    command=test_command, cwd=str(integration_path),
                    duration_ms=test_run.duration_ms,
                    exit_code=test_exit_code,
                    stdout=test_run.stdout, stderr=test_run.stderr,
                )

            bounded_test_output = "\n".join(bounded_lines(test_output, max_lines=200))
            if test_exit_code != 0:
                keep_integration = True
                return self._finish_failure(
                    experiment_id,
                    reason=(
                        "Die Integrationstests sind fehlgeschlagen."
                        if test_exit_code is not None
                        else "Die Integrationstests konnten nicht erfolgreich abgeschlossen werden."
                    ),
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )

            integration_status = _run_git(
                ["status", "--short", "--untracked-files=all"],
                cwd=str(integration_path),
                timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                elog=elog,
                phase="adoption",
            )
            integrated_changed = parse_git_status_short(integration_status.stdout)
            eval_result = evaluate_implementation(
                experiment.plan,
                experiment.goal,
                integrated_changed,
                str(integration_path),
            )
            if eval_result.outcome != "pass":
                keep_integration = True
                return self._finish_review(
                    experiment_id,
                    reason=f"Qualitäts-Gate auf integriertem Stand: {eval_result.reason}",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )

            integrated_commit_proc = _run_git(
                ["commit", "--no-verify", "-m", commit_message],
                cwd=str(integration_path),
                timeout=120,
                elog=elog,
                phase="adoption",
            )
            if integrated_commit_proc.returncode != 0:
                keep_integration = True
                return self._finish_failure(
                    experiment_id,
                    reason="Der Integrations-Commit konnte nicht erstellt werden.",
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )
            integrated_commit_sha = self._git_stdout(["rev-parse", "HEAD"], str(integration_path), elog).strip() or None

            push_proc = _run_git(
                ["push", "origin", f"HEAD:{_DEFAULT_TARGET_BRANCH}"],
                cwd=str(integration_path),
                timeout=120,
                elog=elog,
                phase="adoption",
            )
            if push_proc.returncode != 0:
                keep_integration = True
                combined_push = f"{push_proc.stdout}\n{push_proc.stderr}".lower()
                reason = (
                    "origin/main hat sich geändert, manuelle Prüfung nötig."
                    if "rejected" in combined_push or "non-fast-forward" in combined_push
                    else "Der Push nach origin/main ist fehlgeschlagen."
                )
                return self._finish_review(
                    experiment_id,
                    reason=reason,
                    actual_files=actual_files,
                    expected_files=expected_files,
                    test_exit_code=test_exit_code,
                    test_output=bounded_test_output,
                    experiment_commit_sha=experiment_commit_sha,
                    integrated_commit_sha=integrated_commit_sha,
                    diagnostic_worktree_path=str(integration_path),
                    elog=elog,
                )

            local_sync_pending, adopted_reason = self._sync_local_repo(elog)
            self._set_status(
                experiment_id,
                STATUS_ADOPTED,
                ended_at=_utcnow_iso(),
                experiment_commit_sha=experiment_commit_sha,
                integrated_commit_sha=integrated_commit_sha,
                expected_files_json=json.dumps(expected_files),
                actual_files_json=json.dumps(actual_files),
                test_exit_code=test_exit_code,
                test_output=bounded_test_output,
                reason=sanitized_reason(adopted_reason),
                local_sync_pending=1 if local_sync_pending else 0,
                diagnostic_worktree_path=None,
                updated_at=_utcnow_iso(),
            )
            return self.get(experiment_id)
        except ExperimentAdoptionError as exc:
            return self._finish_failure(experiment_id, reason=str(exc), elog=elog)
        except subprocess.SubprocessError as exc:
            keep_integration = True
            return self._finish_failure(
                experiment_id,
                reason=f"Git-/Test-Prozess fehlgeschlagen: {exc}",
                diagnostic_worktree_path=str(integration_path) if integration_path else None,
                elog=elog,
            )
        finally:
            try:
                if integration_path is not None and integration_path.exists() and not keep_integration:
                    _run_git(
                        ["worktree", "remove", "--force", str(integration_path)],
                        cwd=str(self.repo_root),
                        timeout=120,
                        elog=elog,
                        phase="adoption",
                    )
                    shutil.rmtree(integration_path, ignore_errors=True)
            finally:
                lock.release()

    def _finish_failure(
        self,
        experiment_id: str,
        *,
        reason: str,
        actual_files: list[str] | None = None,
        expected_files: list[str] | None = None,
        test_exit_code: int | None = None,
        test_output: str | None = None,
        experiment_commit_sha: str | None = None,
        integrated_commit_sha: str | None = None,
        diagnostic_worktree_path: str | None = None,
        elog=None,
    ) -> ExperimentAdoption:
        if elog is not None:
            elog.warning("adoption", "adoption_failed", reason)
        self._set_status(
            experiment_id,
            STATUS_FAILED,
            ended_at=_utcnow_iso(),
            experiment_commit_sha=experiment_commit_sha,
            integrated_commit_sha=integrated_commit_sha,
            expected_files_json=json.dumps(_safe_list(expected_files)),
            actual_files_json=json.dumps(_safe_list(actual_files)),
            test_exit_code=test_exit_code,
            test_output=_truncate_text(test_output, 32 * 1024, label="test output"),
            reason=sanitized_reason(reason),
            diagnostic_worktree_path=diagnostic_worktree_path,
            updated_at=_utcnow_iso(),
        )
        return self.get(experiment_id)

    def _finish_review(
        self,
        experiment_id: str,
        *,
        reason: str,
        actual_files: list[str] | None = None,
        expected_files: list[str] | None = None,
        test_exit_code: int | None = None,
        test_output: str | None = None,
        experiment_commit_sha: str | None = None,
        integrated_commit_sha: str | None = None,
        diagnostic_worktree_path: str | None = None,
        elog=None,
    ) -> ExperimentAdoption:
        if elog is not None:
            elog.warning("adoption", "adoption_needs_review", reason)
        self._set_status(
            experiment_id,
            STATUS_NEEDS_REVIEW,
            ended_at=_utcnow_iso(),
            experiment_commit_sha=experiment_commit_sha,
            integrated_commit_sha=integrated_commit_sha,
            expected_files_json=json.dumps(_safe_list(expected_files)),
            actual_files_json=json.dumps(_safe_list(actual_files)),
            test_exit_code=test_exit_code,
            test_output=_truncate_text(test_output, 32 * 1024, label="test output"),
            reason=sanitized_reason(reason),
            diagnostic_worktree_path=diagnostic_worktree_path,
            updated_at=_utcnow_iso(),
        )
        return self.get(experiment_id)

    def _git_stdout(self, args: list[str], cwd: str, elog) -> str:
        proc = _run_git(args, cwd=cwd, timeout=_DEFAULT_GIT_TIMEOUT_SECONDS, elog=elog, phase="adoption")
        return proc.stdout or ""

    def _commit_message(self, experiment_id: str, goal: str, plan: dict[str, Any] | None) -> str:
        title = ""
        if isinstance(plan, dict):
            title = str(plan.get("title") or "")
        summary = _collapse_summary(title or goal)
        return f"Adopt experiment {experiment_id[:8]}: {summary}"

    def _sync_local_repo(self, elog) -> tuple[bool, str]:
        status_proc = _run_git(["status", "--porcelain"], cwd=str(self.repo_root), timeout=30, elog=elog, phase="adoption")
        if status_proc.returncode != 0 or status_proc.stdout.strip():
            return True, "Übernahme veröffentlicht; lokales Repo ist nicht sauber und muss manuell synchronisiert werden."
        branch_proc = _run_git(["branch", "--show-current"], cwd=str(self.repo_root), timeout=30, elog=elog, phase="adoption")
        if branch_proc.returncode != 0 or branch_proc.stdout.strip() != _DEFAULT_TARGET_BRANCH:
            return True, "Übernahme veröffentlicht; lokales Repo ist nicht auf main und wurde nicht automatisch synchronisiert."
        merge_proc = _run_git(["merge", "--ff-only", f"origin/{_DEFAULT_TARGET_BRANCH}"], cwd=str(self.repo_root), timeout=120, elog=elog, phase="adoption")
        if merge_proc.returncode != 0:
            return True, "Übernahme veröffentlicht; lokales Repo konnte nicht per Fast-Forward synchronisiert werden."
        return False, "Experiment erfolgreich übernommen und veröffentlicht."
