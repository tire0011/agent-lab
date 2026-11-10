"""Experiment records and state methods of :class:`ExperimentService`."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import threading
import uuid

from pathlib import Path
from typing import Any, Callable

from modules.central_logging import DEFAULT_LOG_DIR, ExperimentLogger
from modules.codex_worker import CodexWorker
from modules.coding_provider import (
    build_random_worker,
    build_worker,
    random_worker_factory,
)
from modules.redaction import sanitized_reason
from modules.run_store import DEFAULT_DB_PATH, init_db, insert_activity, list_activity

from ..models import (
    Experiment,
    ExperimentAlreadyActiveError,
    ExperimentNotCancellableError,
    ExperimentNotFoundError,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_IMPLEMENTING,
    STATUS_INTERRUPTED,
    STATUS_NEEDS_REVIEW,
    STATUS_QUEUED,
    STATUS_REJECTED,
    STATUS_SELECTING,
    STATUS_SUCCEEDED,
    STATUS_VERIFYING,
    _TERMINAL_STATUSES,
    _row_to_experiment,
)
from ..text_utils import _utcnow_iso
from .core import (
    EXPERIMENT_TOKEN_BUDGET_ENV_VAR,
    EXPERIMENT_TOKEN_BUDGET_MODE_ENV_VAR,
    _DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS,
    _DEFAULT_MAX_DIAGNOSTIC_LOG_BYTES,
    _DEFAULT_MAX_DIFF_BYTES,
    _DEFAULT_MAX_RAW_MESSAGE_BYTES,
    _DEFAULT_MAX_REPAIR_ITERATIONS,
    _DEFAULT_MAX_TEST_OUTPUT_BYTES,
    _DEFAULT_TEST_PYTHON_EXECUTABLE,
    _ERROR_STATUSES,
    _GITHUB_ISSUE_GOAL_RE,
    _INTERRUPTED_BY_RESTART_ERROR,
    _WARNING_STATUSES,
    TOKEN_BUDGET_STOP_PREFIX,
    _experiment_token_budget,
    _is_budget_stop,
    _is_stalled,
    worker_run_token_total,
)


logger = logging.getLogger("modules.experiment_service")

class RecordsMixin:
    """Experiment records and state methods of :class:`ExperimentService`."""

    def __init__(
        self,
        selection_worker: CodexWorker | None = None,
        worker_factory: Callable[[str], CodexWorker] | None = None,
        local_worker_factory: Callable[[str], Any] | None = None,
        plan_worker_factory: Callable[[str], CodexWorker] | None = None,
        db_path: str = DEFAULT_DB_PATH,
        repo_root: str = "/agent",
        worktrees_root: str | None = None,
        max_test_output_bytes: int = _DEFAULT_MAX_TEST_OUTPUT_BYTES,
        max_diff_bytes: int = _DEFAULT_MAX_DIFF_BYTES,
        max_raw_message_bytes: int = _DEFAULT_MAX_RAW_MESSAGE_BYTES,
        max_diagnostic_log_bytes: int = _DEFAULT_MAX_DIAGNOSTIC_LOG_BYTES,
        test_python_executable: str = _DEFAULT_TEST_PYTHON_EXECUTABLE,
        experiment_log_dir: str | None = None,
        test_stall_timeout_seconds: float | None = None,
        test_watchdog_interval_seconds: float | None = None,
        test_safety_cap_seconds: float | None = None,
        max_repair_iterations: int = _DEFAULT_MAX_REPAIR_ITERATIONS,
        rng: Any | None = None,
        base_ref: str = "HEAD",
        test_command: list[str] | None = None,
    ) -> None:
        """
        Parameters
        ----------
        rng:
            Deprecated compatibility argument; ignored when factories are injected.
        selection_worker:
            Worker for the read-only research phase; defaults to one allowed only inside ``repo_root``.
        worker_factory:
            ``(worktree_path) -> worker`` for the implementation phase, scoped to that worktree only.
        plan_worker_factory:
            ``(scratch_dir) -> worker`` for the plan phase, scoped to a fresh empty scratch dir that never sees ``repo_root``.
        repo_root:
            Main repository root (default ``/agent``); never modified by this service.
        worktrees_root:
            Parent dir for per-experiment worktrees and scratch dirs (default ``~/agent-worktrees``).
        test_python_executable:
            Interpreter for verification inside a worktree; defaults to the main repo's venv
            python so no package is installed or copied into the worktree.
        experiment_log_dir:
            Directory for the per-experiment append-only JSONL log; defaults to
            ``<repo_root>/state/experiment-logs``.
        test_stall_timeout_seconds / test_watchdog_interval_seconds / test_safety_cap_seconds:
            Watchdog rules for local verification; ``None`` resolves from the environment
            (clamped, see ``WatchdogConfig.resolve``). The safety cap is only a final guard
            against an orphaned process, never the normal stop mechanism.
        base_ref:
            Commit an experiment's worktree starts from unless the GitHub lifecycle supplies one.
        test_command:
            Verification argv template replacing the built-in suite; ``{python}`` becomes
            ``test_python_executable``. Trusted callers only - never build it from plan or
            model output.
        """
        self.selection_worker = (
            selection_worker
            if selection_worker is not None
            else build_random_worker(db_path=db_path)[1]
        )
        # One provider per worktree/scratch dir, i.e. one agent per experiment: the
        # implementation call and any repair iteration share it, the next experiment rotates.
        self.worker_factory = worker_factory or random_worker_factory(db_path=db_path, rng=rng)
        self.local_worker_factory = local_worker_factory or (
            lambda _working_dir: build_worker("local", db_path=db_path)
        )
        self.plan_worker_factory = plan_worker_factory or random_worker_factory(db_path=db_path, rng=rng)
        self.db_path = db_path
        self.repo_root = repo_root
        self.worktrees_root = worktrees_root or str(Path.home() / "agent-worktrees")
        self.max_test_output_bytes = max_test_output_bytes
        self.max_diff_bytes = max_diff_bytes
        self.max_raw_message_bytes = max_raw_message_bytes
        self.max_diagnostic_log_bytes = max_diagnostic_log_bytes
        self.test_python_executable = test_python_executable
        self.test_stall_timeout_seconds = test_stall_timeout_seconds
        self.test_watchdog_interval_seconds = test_watchdog_interval_seconds
        self.test_safety_cap_seconds = test_safety_cap_seconds
        self.max_repair_iterations = max(0, max_repair_iterations)
        self.experiment_token_budget = _experiment_token_budget(
            os.environ.get(EXPERIMENT_TOKEN_BUDGET_ENV_VAR)
        )
        self.experiment_token_budget_mode = (
            "enforce" if os.environ.get(EXPERIMENT_TOKEN_BUDGET_MODE_ENV_VAR, "").strip().lower() == "enforce"
            else "shadow"
        )
        self.base_ref = base_ref
        self.test_command = test_command
        # Deliberately *not* derived from ``repo_root``: tests point ``repo_root`` at a
        # throwaway checkout whose cleanliness is asserted directly, so the log directory
        # must never live inside it.
        self.experiment_log_dir = experiment_log_dir or DEFAULT_LOG_DIR

        conn = init_db(db_path)
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._lock = threading.Lock()
        self._active_processes: dict[str, subprocess.Popen] = {}
        self._process_lock = threading.Lock()


    def close(self) -> None:
        self._conn.close()


    def _track_process(self, experiment_id: str, process: subprocess.Popen | None) -> None:
        with self._process_lock:
            if process is None:
                self._active_processes.pop(experiment_id, None)
            else:
                self._active_processes[experiment_id] = process
        # Workers run in their own process group (start_new_session); the id is persisted so
        # a stall sweep in another process can stop it (#400).
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE experiments SET worker_pgid = ? WHERE id = ?",
                (process.pid if process is not None else None, experiment_id),
            )
        # Close the race where cancellation (or the stall sweep) commits after a phase starts
        # but before it publishes its Popen handle.
        if process is not None and self._ended_from_outside(experiment_id):
            self._terminate_process(process)


    def _ended_from_outside(self, experiment_id: str) -> bool:
        """Cancelled by an operator, failed by the stall sweep, or stopped by the
        token budget in enforce mode (#533) while running.

        The pipeline thread checks this after every blocking step and stops instead of
        overwriting the outcome or starting another paid run.
        """
        experiment = self.get_experiment(experiment_id)
        return (
            experiment.status == STATUS_CANCELLED
            or _is_stalled(experiment.status, experiment.error)
            or _is_budget_stop(experiment.status, experiment.error)
        )


    def _stop_for_token_budget(
        self, experiment_id: str, total: int, stop_reason: str
    ) -> None:
        """End the experiment in ``needs_review`` because enforce mode's budget is crossed.

        An outcome set from outside (cancel, stall sweep, an earlier budget stop) wins.
        """
        if self._ended_from_outside(experiment_id):
            return
        self._force_status(
            experiment_id, STATUS_NEEDS_REVIEW,
            ended_at=_utcnow_iso(), implementation_ended_at=_utcnow_iso(),
            error=(
                f"{TOKEN_BUDGET_STOP_PREFIX}: stop_reason={stop_reason}, "
                f"budget={self.experiment_token_budget}, consumed={total}"
            ),
        )


    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait(timeout=2)


    def cancel_experiment(self, experiment_id: str, cancelled_by: str) -> Experiment:
        """Cancel an active implementation or verification and retain its worktree."""
        experiment = self.resolve_experiment(experiment_id)
        actor = (cancelled_by or "api").strip()[:200] or "api"
        now = _utcnow_iso()
        moved = False
        for status in (STATUS_IMPLEMENTING, STATUS_VERIFYING):
            if self._transition(
                experiment.id, status, STATUS_CANCELLED,
                ended_at=now, cancelled_at=now, cancelled_by=actor,
                error=f"cancelled by {actor}",
            ):
                moved = True
                break
        if not moved:
            current = self.get_experiment(experiment.id)
            raise ExperimentNotCancellableError(
                f"experiment {experiment.id!r} cannot be cancelled from status {current.status!r}"
            )

        with self._process_lock:
            process = self._active_processes.get(experiment.id)
        if process is not None:
            self._terminate_process(process)
        return self.get_experiment(experiment.id)


    def create_experiment(
        self,
        goal: str,
        timeout_seconds: int = _DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS,
        complexity: str | None = None,
        source_type: str | None = None,
        source_id: str | None = None,
    ) -> Experiment:
        if not goal or not goal.strip():
            raise ValueError("goal must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        normalized_goal = goal.strip()
        issue_match = _GITHUB_ISSUE_GOAL_RE.match(normalized_goal)
        issue_number = int(issue_match.group(1)) if issue_match is not None else None
        normalized_complexity = "small" if complexity == "small" else "large"
        if source_type is None and issue_number is not None:
            source_type, source_id = "issue", str(issue_number)
        normalized_source_type = str(source_type).strip()[:64] if source_type else None
        normalized_source_id = str(source_id).strip()[:256] if source_id else None
        experiment_id = uuid.uuid4().hex
        created_at = _utcnow_iso()

        with self._lock:
            # BEGIN IMMEDIATE reserves the writer before the SELECT: a deferred transaction would
            # start only at the INSERT, letting two instances both pass the check before either
            # owns the write lock.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if issue_number is not None:
                    placeholders = ", ".join("?" for _ in _TERMINAL_STATUSES)
                    active_rows = self._conn.execute(
                        f"""
                        SELECT id, status, goal FROM experiments
                        WHERE status NOT IN ({placeholders})
                        ORDER BY created_at ASC
                        """,
                        _TERMINAL_STATUSES,
                    ).fetchall()
                    active = next(
                        (
                            row
                            for row in active_rows
                            if (match := _GITHUB_ISSUE_GOAL_RE.match(row["goal"]))
                            and int(match.group(1)) == issue_number
                        ),
                        None,
                    )
                    if active is not None:
                        raise ExperimentAlreadyActiveError(active["id"], active["status"])
                self._conn.execute(
                    """
                    INSERT INTO experiments
                        (id, goal, status, created_at, complexity, source_type, source_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (experiment_id, normalized_goal, STATUS_QUEUED, created_at,
                     normalized_complexity, normalized_source_type, normalized_source_id),
                )
                insert_activity(
                    self._conn,
                    timestamp=created_at,
                    entity_type="experiment",
                    entity_id=experiment_id,
                    event="created",
                    to_status=STATUS_QUEUED,
                    message=normalized_goal,
                )
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

        logger.info("experiment %s created (status=%s)", experiment_id, STATUS_QUEUED)
        self.get_experiment_logger(experiment_id).info(
            STATUS_QUEUED, "experiment_created",
            f"Experiment created: {normalized_goal}",
            timeout_seconds=timeout_seconds,
        )
        return self.get_experiment(experiment_id)


    def _must_use_external_provider(self, experiment: Experiment) -> bool:
        """Whether a prior local attempt for this stable task identity did not succeed."""
        if not experiment.source_type or not experiment.source_id:
            return False
        failed_terminal = tuple(status for status in _TERMINAL_STATUSES if status != STATUS_SUCCEEDED)
        placeholders = ", ".join("?" for _ in failed_terminal)
        with self._lock:
            row = self._conn.execute(
                f"""SELECT 1 FROM experiments
                   WHERE source_type = ? AND source_id = ? AND id != ?
                     AND implementation_provider = 'local' AND status IN ({placeholders})
                   LIMIT 1""",
                (experiment.source_type, experiment.source_id, experiment.id, *failed_terminal),
            ).fetchone()
        return row is not None


    def get_experiment(self, experiment_id: str) -> Experiment:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
        if row is None:
            raise ExperimentNotFoundError(f"no experiment with id {experiment_id!r}")
        return _row_to_experiment(row)


    def _resolve_by_prefix(self, experiment_id: str) -> Experiment:
        """Resolve an exact id or an unambiguous >=8 hex-char id prefix.

        Shared by the public and authenticated single-experiment lookups so the same
        short id works against either endpoint.
        """
        if re.fullmatch(r"[0-9a-f]{8,64}", experiment_id) is None:
            raise ExperimentNotFoundError("invalid experiment id")
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM experiments WHERE id LIKE ? ORDER BY id LIMIT 2",
                (experiment_id + "%",),
            ).fetchall()
        if len(rows) != 1:
            raise ExperimentNotFoundError("experiment id is missing or ambiguous")
        return _row_to_experiment(rows[0])


    def resolve_public_experiment(self, experiment_id: str) -> Experiment:
        """Resolve an exact id or a unique public prefix of at least 8 hex chars."""
        return self._resolve_by_prefix(experiment_id)


    def resolve_experiment(self, experiment_id: str) -> Experiment:
        """Authenticated equivalent of ``resolve_public_experiment``.

        Accepts an exact id or an unambiguous >=8 hex-char prefix like the public endpoint;
        ids that are not valid hex prefixes fall back to an exact-id lookup.
        """
        if re.fullmatch(r"[0-9a-f]{8,64}", experiment_id) is not None:
            return self._resolve_by_prefix(experiment_id)
        return self.get_experiment(experiment_id)


    def list_experiments(self, limit: int = 20) -> list[Experiment]:
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM experiments ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_experiment(row) for row in rows]


    def list_gate_recheck_candidates(self) -> list[Experiment]:
        """Return only terminal runs eligible for a read-only gate recheck."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM experiments
                WHERE status = ?
                   OR (status = ? AND error LIKE 'quality evaluation:%')
                ORDER BY created_at DESC, id DESC
                """,
                (STATUS_REJECTED, STATUS_NEEDS_REVIEW),
            ).fetchall()
        return [_row_to_experiment(row) for row in rows]


    def list_activity(
        self, limit: int = 50, experiment_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return recent persisted activity log entries, newest first.

        Reuses ``modules.run_store.list_activity`` on the same connection/lock as all
        other experiment reads/writes.
        """
        with self._lock:
            return list_activity(
                self._conn,
                limit=limit,
                entity_type="experiment",
                entity_id=experiment_id,
            )


    def mailbox_task_id_for_experiment(self, experiment_id: str) -> str | None:
        """Return the approval id linked to an experiment, if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM mailbox_task_approvals WHERE experiment_id = ? LIMIT 1",
                (experiment_id,),
            ).fetchone()
        return str(row[0]) if row else None


    def _record_worker_run_usage(
        self,
        experiment_id: str,
        usage: dict[str, Any] | None,
        elog: ExperimentLogger,
    ) -> str | None:
        """Accumulate one finished implementation/fallback/repair run.

        Without a configured budget this returns the historical raw usage JSON
        without changing the database. With a budget, accounting and the
        warning-deduplication flag are committed atomically.
        """
        if self.experiment_token_budget is None:
            return json.dumps(usage) if usage else None

        run_tokens = worker_run_token_total(usage)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT implementation_usage_json FROM experiments WHERE id = ?",
                (experiment_id,),
            ).fetchone()
            previous: dict[str, Any] = {}
            if row is not None and row[0]:
                try:
                    decoded = json.loads(row[0])
                    if isinstance(decoded, dict):
                        previous = decoded
                except (TypeError, json.JSONDecodeError):
                    pass
            total = int(previous.get("tokens_consumed", 0)) + run_tokens
            run_count = int(previous.get("token_run_count", 0)) + 1
            already_warned = bool(previous.get("token_budget_warning_logged", False))
            over = total > self.experiment_token_budget
            crossed = over and not already_warned
            persisted = dict(usage) if isinstance(usage, dict) else {}
            persisted.update(
                token_budget=self.experiment_token_budget,
                tokens_consumed=total,
                token_run_count=run_count,
                token_budget_warning_logged=already_warned or crossed,
            )
            encoded = json.dumps(persisted)
            self._conn.execute(
                "UPDATE experiments SET implementation_usage_json = ? WHERE id = ?",
                (encoded, experiment_id),
            )

        enforcing = self.experiment_token_budget_mode == "enforce"
        if crossed:
            elog.warning(
                "implementation",
                "token_budget_exceeded",
                (
                    "Experiment token budget exceeded at the end of a worker run; "
                    "experiment ends for review"
                    if enforcing else
                    "Experiment token budget exceeded (shadow mode; execution continues)"
                ),
                budget=self.experiment_token_budget,
                total=total,
                run_count=run_count,
                mode=self.experiment_token_budget_mode,
            )
        else:
            # ``nexus logs`` reads this same structured stream, so the current
            # shadow total is visible even before crossing (and after the one
            # deduplicated warning has already been emitted).
            elog.info(
                "implementation",
                "token_budget_updated",
                "Experiment token usage updated (shadow mode)",
                budget=self.experiment_token_budget,
                total=total,
                run_count=run_count,
            )
        if enforcing and over:
            # Codex reports usage only on ``turn.completed`` (and Claude only when no
            # live count was possible), so the run has already finished here.
            self._stop_for_token_budget(experiment_id, total, "end_of_run_count")
        return encoded


    def _current_tokens_consumed(self, experiment_id: str) -> int:
        usage = self.get_experiment(experiment_id).implementation_usage
        if isinstance(usage, dict):
            try:
                return int(usage.get("tokens_consumed", 0))
            except (TypeError, ValueError):
                return 0
        return 0


    def _add_live_token_usage(
        self,
        experiment_id: str,
        usage: dict[str, int],
        tokens: int,
        *,
        new_run: bool,
        live_calls: int,
    ) -> tuple[int, int, bool, bool]:
        """Add live-counted usage atomically; return ``(total, run_count, first_crossing, over)``.

        Unlike the end-of-run record, per-counter values are summed, because one run
        arrives here in several pieces (one per API call, #533).
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT implementation_usage_json FROM experiments WHERE id = ?",
                (experiment_id,),
            ).fetchone()
            previous: dict[str, Any] = {}
            if row is not None and row[0]:
                try:
                    decoded = json.loads(row[0])
                    if isinstance(decoded, dict):
                        previous = decoded
                except (TypeError, json.JSONDecodeError):
                    pass
            total = int(previous.get("tokens_consumed", 0)) + tokens
            run_count = int(previous.get("token_run_count", 0)) + (1 if new_run else 0)
            already_warned = bool(previous.get("token_budget_warning_logged", False))
            over = total > self.experiment_token_budget
            for key, value in usage.items():
                previous[key] = int(previous.get(key, 0)) + int(value)
            previous.update(
                token_budget=self.experiment_token_budget,
                tokens_consumed=total,
                token_run_count=run_count,
                token_budget_warning_logged=already_warned or over,
                live_api_call_count=int(previous.get("live_api_call_count", 0)) + live_calls,
            )
            self._conn.execute(
                "UPDATE experiments SET implementation_usage_json = ? WHERE id = ?",
                (json.dumps(previous), experiment_id),
            )
        return total, run_count, over and not already_warned, over


    def _live_worker_usage_callback(
        self, experiment_id: str, elog: ExperimentLogger
    ) -> tuple[Callable[[dict[str, Any]], bool] | None, dict[str, Any]]:
        """Build a per-run Claude usage observer (#533).

        ``state["tokens"]``/``state["calls"]`` count only what is committed to the
        database. Usage whose write failed stays in ``pending_*`` and is retried with
        the next call and once more by :meth:`_finish_live_usage` at the end of the
        run, so a transient error never loses usage. The observer never raises, and
        in enforce mode its stop decision does not depend on the database: when the
        write fails it uses the experiment total from before the run plus every call
        seen so far (fail closed).
        """
        state: dict[str, Any] = {
            "tokens": 0, "calls": 0,
            "pending_usage": {}, "pending_tokens": 0, "pending_calls": 0,
            "seen_tokens": 0, "run_counted": False, "failures": 0,
            "stop_requested": False, "base_total": 0,
        }
        if self.experiment_token_budget is None:
            return None, state
        try:
            state["base_total"] = self._current_tokens_consumed(experiment_id)
        except Exception:  # noqa: BLE001 - the database is re-read on every call
            pass
        enforcing = self.experiment_token_budget_mode == "enforce"

        def observe(usage: dict[str, Any]) -> bool:
            try:
                tokens = worker_run_token_total(usage)
                flat = {
                    key: int(value) for key, value in (usage or {}).items()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                }
            except Exception:  # noqa: BLE001 - malformed usage counts as zero, see end of run
                tokens, flat = 0, {}
            state["seen_tokens"] += tokens
            for key, value in flat.items():
                state["pending_usage"][key] = state["pending_usage"].get(key, 0) + value
            state["pending_tokens"] += tokens
            state["pending_calls"] += 1

            accounted = False
            try:
                total, run_count, first_crossing, over = self._add_live_token_usage(
                    experiment_id, state["pending_usage"], state["pending_tokens"],
                    new_run=not state["run_counted"], live_calls=state["pending_calls"],
                )
                accounted = True
                state["tokens"] += state["pending_tokens"]
                state["calls"] += state["pending_calls"]
                state["run_counted"] = True
                state["pending_usage"], state["pending_tokens"], state["pending_calls"] = {}, 0, 0
            except Exception as exc:  # noqa: BLE001 - keep the usage pending and decide anyway
                state["failures"] += 1
                total = int(state["base_total"]) + int(state["seen_tokens"])
                run_count = None
                over = total > self.experiment_token_budget
                first_crossing = over
                error = f"{type(exc).__name__}: {exc}"

            if enforcing and over:
                # Stop the worker first; recording the outcome may fail and is retried
                # at the end of the run.
                state["stop_requested"] = True
                try:
                    self._stop_for_token_budget(experiment_id, total, "live_count_worker_stopped")
                except Exception:  # noqa: BLE001 - _finish_live_usage records it again
                    pass

            try:
                if not accounted:
                    elog.warning(
                        "implementation", "token_budget_accounting_failed",
                        "Live token accounting failed; usage kept and retried",
                        error=error, estimated_total=total,
                        pending_tokens=state["pending_tokens"],
                        budget=self.experiment_token_budget,
                        mode=self.experiment_token_budget_mode,
                    )
                if first_crossing and accounted:
                    elog.warning(
                        "implementation", "token_budget_exceeded",
                        (
                            "Experiment token budget exceeded; worker stopped for review"
                            if enforcing else
                            "Experiment token budget exceeded (shadow mode; execution continues)"
                        ),
                        budget=self.experiment_token_budget, total=total,
                        run_count=run_count, mode=self.experiment_token_budget_mode, live=True,
                    )
                elif accounted and not over:
                    elog.info(
                        "implementation", "token_budget_updated",
                        f"Experiment token usage updated ({self.experiment_token_budget_mode} mode)",
                        budget=self.experiment_token_budget, total=total,
                        run_count=run_count, live=True,
                    )
            except Exception:  # noqa: BLE001 - logging must not change the decision
                pass
            return bool(state["stop_requested"])

        return observe, state


    def _finish_live_usage(
        self,
        experiment_id: str,
        final_usage: dict[str, Any] | None,
        state: dict[str, Any],
        elog: ExperimentLogger,
    ) -> str | None:
        """Close the books for a run that was (partly) counted live (#533).

        Adds whatever the live count could not commit: the pending usage, or - when
        the run's final usage reports more than was committed live - the difference
        (whichever is larger). Then enforce mode is applied again, so a live stop
        whose status write failed, or a crossing only seen here, still ends the
        experiment. Errors propagate; the pipeline then fails instead of going on.
        """
        final_tokens = worker_run_token_total(final_usage)
        extra = max(int(state["pending_tokens"]), final_tokens - int(state["tokens"]))
        if extra > 0 or state["pending_calls"]:
            usage = dict(state["pending_usage"])
            unattributed = extra - int(state["pending_tokens"])
            if unattributed > 0:
                usage["end_of_run_reconciled_tokens"] = unattributed
            total, run_count, first_crossing, over = self._add_live_token_usage(
                experiment_id, usage, extra,
                new_run=not state["run_counted"], live_calls=int(state["pending_calls"]),
            )
            state["tokens"] += extra
            state["calls"] += int(state["pending_calls"])
            state["run_counted"] = True
            state["pending_usage"], state["pending_tokens"], state["pending_calls"] = {}, 0, 0
            elog.info(
                "implementation", "token_budget_reconciled",
                "Live token count completed at the end of the worker run",
                added=extra, total=total, run_count=run_count,
                budget=self.experiment_token_budget,
            )
            if first_crossing:
                elog.warning(
                    "implementation", "token_budget_exceeded",
                    (
                        "Experiment token budget exceeded at the end of a worker run; "
                        "experiment ends for review"
                        if self.experiment_token_budget_mode == "enforce" else
                        "Experiment token budget exceeded (shadow mode; execution continues)"
                    ),
                    budget=self.experiment_token_budget, total=total,
                    run_count=run_count, mode=self.experiment_token_budget_mode,
                )
        else:
            total = self._current_tokens_consumed(experiment_id)
        if self.experiment_token_budget_mode == "enforce" and (
            state["stop_requested"] or total > self.experiment_token_budget
        ):
            self._stop_for_token_budget(
                experiment_id, total,
                "live_count_worker_stopped" if state["stop_requested"] else "end_of_run_count",
            )
        usage = self.get_experiment(experiment_id).implementation_usage
        return json.dumps(usage) if usage else None


    def _transition(self, experiment_id: str, from_status: str, to_status: str, **fields: Any) -> bool:
        """Atomically move ``from_status`` -> ``to_status`` iff still ``from_status``.

        Returns True if the transition happened; extra ``fields`` are set in the same
        UPDATE. Disallowed transitions are refused by the caller via ``_STATE_TRANSITIONS``.
        """
        set_clauses = ["status = ?"]
        values: list[Any] = [to_status]
        for key, value in fields.items():
            set_clauses.append(f"{key} = ?")
            values.append(value)
        values.extend([experiment_id, from_status])

        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"UPDATE experiments SET {', '.join(set_clauses)} "
                "WHERE id = ? AND status = ?",
                values,
            )
            moved = cursor.rowcount == 1
            if moved:
                insert_activity(
                    self._conn,
                    timestamp=_utcnow_iso(),
                    entity_type="experiment",
                    entity_id=experiment_id,
                    event="status_changed",
                    from_status=from_status,
                    to_status=to_status,
                    message=sanitized_reason(str(fields["error"])) if fields.get("error") else None,
                )
        if moved:
            level = logging.WARNING if to_status in _WARNING_STATUSES else logging.INFO
            logger.log(
                level,
                "experiment %s: %s -> %s%s",
                experiment_id,
                from_status,
                to_status,
                f" (policy_rule={fields['policy_rule']})" if fields.get("policy_rule") else "",
            )
            elog = self.get_experiment_logger(experiment_id)
            elog_method = (
                elog.error if to_status in _ERROR_STATUSES
                else elog.warning if to_status in _WARNING_STATUSES
                else elog.info
            )
            elog_method(
                to_status, "status_changed",
                f"Experiment transitioned {from_status} -> {to_status}",
                from_status=from_status,
                to_status=to_status,
                policy_rule=fields.get("policy_rule"),
                reason=sanitized_reason(str(fields["error"])) if fields.get("error") else None,
            )
        return moved


    def _force_status(self, experiment_id: str, status: str, **fields: Any) -> None:
        """Set status unconditionally (used for terminal/failure bookkeeping)."""
        set_clauses = ["status = ?"]
        values: list[Any] = [status]
        for key, value in fields.items():
            set_clauses.append(f"{key} = ?")
            values.append(value)
        values.append(experiment_id)

        with self._lock, self._conn:
            current = self._conn.execute(
                "SELECT status, error FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
            if current is not None and current[0] == STATUS_CANCELLED and status != STATUS_CANCELLED:
                return
            # The pipeline thread of a stalled experiment may still wake up once its worker is
            # stopped; the stall verdict stands (#400).
            if current is not None and _is_stalled(current[0], current[1]):
                return
            self._conn.execute(
                f"UPDATE experiments SET {', '.join(set_clauses)} WHERE id = ?",
                values,
            )
            insert_activity(
                self._conn,
                timestamp=_utcnow_iso(),
                entity_type="experiment",
                entity_id=experiment_id,
                event="status_forced",
                to_status=status,
                message=sanitized_reason(str(fields["error"])) if fields.get("error") else None,
            )
        level = logging.WARNING if status in _WARNING_STATUSES else logging.INFO
        logger.log(
            level,
            "experiment %s: forced -> %s%s",
            experiment_id,
            status,
            f" (policy_rule={fields['policy_rule']})" if fields.get("policy_rule") else "",
        )
        elog = self.get_experiment_logger(experiment_id)
        elog_method = (
            elog.error if status in _ERROR_STATUSES
            else elog.warning if status in _WARNING_STATUSES
            else elog.info
        )
        elog_method(
            status, "status_forced",
            f"Experiment status forced to {status}",
            to_status=status,
            policy_rule=fields.get("policy_rule"),
            reason=sanitized_reason(str(fields["error"])) if fields.get("error") else None,
        )


    def mark_interrupted_experiments_failed(self) -> list[str]:
        """Reconcile experiments orphaned by a service restart (#281).

        An experiment runs in a daemon thread of the ``agent-api`` process, so a restart
        kills it mid-flight and leaves its row active forever. Called once at process
        startup, before any new experiment runs. Never resumes the work: moving to a
        terminal status is what lets the waiting Nexus session resume and decide.
        Implementation/verification become ``interrupted`` (legacy ``selecting`` becomes
        ``failed``); worktrees are deliberately retained for inspection or retry.

        Returns the list of reconciled experiment ids.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, status FROM experiments WHERE status IN (?, ?, ?)",
                (STATUS_SELECTING, STATUS_IMPLEMENTING, STATUS_VERIFYING),
            ).fetchall()
        marked: list[str] = []
        for row in rows:
            # Conditional per row: a status that moved on in between is left alone.
            terminal_status = (
                STATUS_FAILED if row["status"] == STATUS_SELECTING else STATUS_INTERRUPTED
            )
            if self._transition(
                row["id"], row["status"], terminal_status,
                ended_at=_utcnow_iso(),
                error=_INTERRUPTED_BY_RESTART_ERROR,
            ):
                # Clearing the registry also makes in-process startup reconciliation safe and prevents
                # stale cancellation targets.
                with self._process_lock:
                    self._active_processes.pop(row["id"], None)
                marked.append(row["id"])
        return marked
