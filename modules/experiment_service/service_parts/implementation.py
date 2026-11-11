"""Implementation runs (workers, provider fallback, repair) of :class:`ExperimentService`."""

from __future__ import annotations

import re
import subprocess
import uuid

from pathlib import Path
from typing import Any

from modules.central_logging import ExperimentLogger
from modules.codex_worker import CodexResult
from modules.coding_provider import (
    build_worker,
    is_provider_overload,
    other_provider,
    provider_of,
)
from modules.coding_worker import agent_watchdog_limits
from modules.experiment_handover import model_written_tests_block, with_model_written_tests
from modules.local_coding_worker import is_context_too_small

from ..git_utils import _run_git
from ..models import (
    Experiment,
    ExperimentNotRunnableError,
    PIPELINE_CRASH_ERROR_PREFIX,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_IMPLEMENTING,
    STATUS_NEEDS_REVIEW,
    STATUS_QUEUED,
    STATUS_REJECTED,
    STATUS_SUCCEEDED,
    STATUS_TIMED_OUT,
    STATUS_VERIFYING,
)
from ..policy import _check_goal_policy, _goal_secret_warning, _policy_rule_for_reason
from ..prompts import (
    _build_direct_implementation_prompt,
    _build_repair_implementation_prompt,
)
from ..text_utils import _truncate_text, _utcnow_iso
from modules.redaction import sanitized_reason


def _take_model_written_tests(worker: Any, worktree_path: str) -> str:
    """Hand the local worker's generated test file over instead of leaving it (#636).

    The file is removed from the worktree, so a paid coder that ignores it does not
    ship it: it reaches the final diff only when that coder writes it again. Returns
    the marked block, empty when the local attempt kept no generated tests.
    """
    rel = getattr(worker, "generated_test_file", None)
    content = getattr(worker, "generated_test_content", "") or ""
    tests = tuple(getattr(worker, "generated_tests", ()) or ())
    if not rel or not content or not tests:
        return ""
    root = Path(worktree_path).resolve()
    target = root / str(rel)
    try:
        inside = root in target.parent.resolve().parents or target.parent.resolve() == root
        # The local worker created this file (it never overwrites one), so any
        # version of it is model-written, not work to keep.
        if inside and target.is_file() and not target.is_symlink():
            target.unlink()
    except OSError:
        pass
    return model_written_tests_block(str(rel), content, tests)


def _report_issue_progress(
    github_work_tracking: Any | None,
    experiment_id: str,
    state: str,
    detail: str | None = None,
) -> None:
    """Best-effort Issue progress comment; reporting never affects the run."""
    post = getattr(github_work_tracking, "post_progress", None)
    if post is None:
        return
    try:
        post(experiment_id, state, detail=detail)
    except Exception:  # noqa: BLE001 - lifecycle reporting is informational
        return


def _final_issue_progress(experiment: Experiment) -> tuple[tuple[str, str | None], ...]:
    """Map a terminal experiment result to concise Issue lifecycle outcomes."""
    if experiment.status == STATUS_SUCCEEDED:
        match = re.search(r"\b\d+ (?:passed|failed)\b[^\n=]*", experiment.test_output or "")
        detail = f"Tests: {match.group(0).strip()}." if match else None
        return (("verification_passed", detail), ("completed", None))
    reason = sanitized_reason(experiment.error)
    detail = f"Reason: {reason}" if reason else None
    if experiment.status == STATUS_NEEDS_REVIEW:
        return (("needs_review", detail),)
    if experiment.status in {STATUS_FAILED, STATUS_TIMED_OUT, STATUS_REJECTED}:
        return (("failed", detail),)
    return ()


def _last_logged_phase(elog: Any) -> str | None:
    """The phase of the last entry the pipeline logged before it raised (#410)."""
    try:
        from modules.central_logging import read_log_entries

        entries = read_log_entries(elog.path, tail_bytes=64 * 1024)
    except Exception:  # noqa: BLE001 - diagnostics must not mask the crash
        return None
    for entry in reversed(entries):
        phase = str(entry.get("phase") or "")
        if phase and phase != "pipeline":
            return phase[:64]
    return None


class ImplementationMixin:
    """Implementation runs (workers, provider fallback, repair) of :class:`ExperimentService`."""

    def run_experiment(self, experiment_id: str, github_work_tracking: Any | None = None) -> Experiment:
        experiment = self.get_experiment(experiment_id)  # raises if missing
        elog = self.get_experiment_logger(experiment_id)

        if experiment.status != STATUS_QUEUED:
            elog.warning(
                experiment.status, "run_experiment_rejected",
                f"run_experiment called but experiment is not queued (status={experiment.status})",
            )
            raise ExperimentNotRunnableError(
                f"experiment {experiment_id!r} cannot be started from status "
                f"{experiment.status!r} (only {STATUS_QUEUED!r} experiments can run)"
            )

        elog.info(STATUS_QUEUED, "run_experiment_started", "Experiment pipeline starting")

        moved = self._transition(
            experiment_id, STATUS_QUEUED, STATUS_IMPLEMENTING,
            implementation_started_at=_utcnow_iso(),
        )
        if not moved:
            # Raced with another start attempt; refuse rather than double-run.
            current = self.get_experiment(experiment_id)
            elog.warning(
                current.status, "run_experiment_race_lost",
                f"run_experiment lost a race; experiment already status={current.status}",
            )
            raise ExperimentNotRunnableError(
                f"experiment {experiment_id!r} cannot be started from status "
                f"{current.status!r} (only {STATUS_QUEUED!r} experiments can run)"
            )

        _report_issue_progress(github_work_tracking, experiment_id, "started")
        _report_issue_progress(github_work_tracking, experiment_id, "implementing")
        try:
            result = self._run_pipeline(experiment_id, github_work_tracking=github_work_tracking)
        except Exception as exc:  # noqa: BLE001 - must never leave an active status
            elog.error(
                "pipeline", "unhandled_exception",
                "Unhandled exception in experiment pipeline; forcing status=failed",
                exc=exc,
            )
            phase = _last_logged_phase(elog)
            failure_type = type(exc).__name__
            self._force_status(
                experiment_id,
                STATUS_FAILED,
                ended_at=_utcnow_iso(),
                # The prefix stays first: it marks a pipeline crash (#467).
                error=f"{PIPELINE_CRASH_ERROR_PREFIX}{failure_type} in phase {phase or 'unknown'}: {exc}",
                failure_phase=phase,
                failure_type=failure_type,
            )
            result = self.get_experiment(experiment_id)
        for state, detail in _final_issue_progress(result):
            _report_issue_progress(github_work_tracking, experiment_id, state, detail)
        return result


    def _run_pipeline(self, experiment_id: str, *, github_work_tracking: Any | None = None) -> Experiment:
        """Single-call implementation pipeline.

        Phases: 1. goal security check (deterministic, no LLM call), 2. isolated git
        worktree creation, 3. one coding-agent call inside the worktree, 4. verification
        (git diff + tests + deterministic quality gate).

        The injected GitHub lifecycle publishes a checkpoint through a temporary index
        before verification; it never changes this worktree's HEAD/index and never
        merges or deploys from this service.
        """
        experiment = self.get_experiment(experiment_id)
        elog = self.get_experiment_logger(experiment_id)

        # Security check: apply the safety denylist to the raw goal.
        rejection_reason = _check_goal_policy(experiment.goal)
        if rejection_reason is not None:
            elog.warning(
                "implementing", "goal_policy_rejected",
                f"Goal rejected by safety policy before any LLM call: {rejection_reason}",
                policy_rule=_policy_rule_for_reason(rejection_reason),
            )
            self._force_status(
                experiment_id, STATUS_REJECTED,
                ended_at=_utcnow_iso(),
                error=rejection_reason,
                policy_rule=_policy_rule_for_reason(rejection_reason),
            )
            return self.get_experiment(experiment_id)
        secret_warning = _goal_secret_warning(experiment.goal)
        if secret_warning is not None:
            elog.warning(
                "implementing", "goal_policy_warning",
                f"Goal may describe a secret action; continuing, the reviewer checks the change: {secret_warning}",
            )

        branch_name = f"experiment/{experiment_id}"
        worktree_path = str(Path(self.worktrees_root) / experiment_id)
        base_ref = self.base_ref
        if github_work_tracking is not None:
            base_ref = github_work_tracking.experiment_base_sha(experiment_id)

        elog.debug(
            "implementation", "worktree_check",
            f"Creating isolated git worktree at branch {branch_name} from {base_ref}",
        )
        try:
            Path(self.worktrees_root).mkdir(parents=True, exist_ok=True)
            create_proc = _run_git(
                ["worktree", "add", "-b", branch_name, worktree_path, base_ref],
                cwd=self.repo_root,
                timeout=60,
                elog=elog,
                phase="implementation",
            )
            if create_proc.returncode != 0:
                self._force_status(
                    experiment_id, STATUS_FAILED,
                    ended_at=_utcnow_iso(),
                    implementation_ended_at=_utcnow_iso(),
                    worktree_path=worktree_path,
                    branch_name=branch_name,
                    error=f"git worktree add failed: {create_proc.stderr.strip()[:2000]}",
                )
                return self.get_experiment(experiment_id)
        except (OSError, subprocess.SubprocessError) as exc:
            elog.error(
                "implementation", "worktree_creation_exception",
                "Exception while creating git worktree", exc=exc,
            )
            self._force_status(
                experiment_id, STATUS_FAILED,
                ended_at=_utcnow_iso(),
                implementation_ended_at=_utcnow_iso(),
                error=f"failed to create worktree: {exc}",
            )
            return self.get_experiment(experiment_id)

        elog.info(
            "implementation", "worktree_created",
            f"Git worktree created at {branch_name}",
        )
        base_sha_proc = _run_git(
            ["rev-parse", "HEAD"], cwd=worktree_path, timeout=30,
            elog=elog, phase="implementation",
        )
        base_commit_sha = base_sha_proc.stdout.strip() if base_sha_proc.returncode == 0 else None
        self._force_status(
            experiment_id, STATUS_IMPLEMENTING,
            worktree_path=worktree_path,
            branch_name=branch_name,
            base_commit_sha=base_commit_sha,
        )

        elog.info(
            "implementation", "implementation_started",
            "Starting implementation (single coding-agent call: analyse, plan, implement)",
        )
        implementation_prompt = _build_direct_implementation_prompt(experiment.goal)
        if github_work_tracking is not None:
            implementation_prompt = self._merge_current_main(worktree_path, elog) + implementation_prompt
        implementation_task_id = uuid.uuid4().hex
        use_local = (
            experiment.complexity == "small"
            and not self._must_use_external_provider(experiment)
        )
        worker = self.local_worker_factory(worktree_path) if use_local else self.worker_factory(worktree_path)
        # Record which coding agent got this experiment before the call, so a timeout or
        # crash still leaves the run attributable.
        implementation_provider = provider_of(worker)
        self._force_status(
            experiment_id, STATUS_IMPLEMENTING,
            implementation_provider=implementation_provider,
        )
        elog.info(
            "implementation", "implementation_provider_selected",
            f"Implementation runs on coding agent {implementation_provider or 'unknown'}",
            provider=implementation_provider or "",
        )

        stall_seconds, max_seconds = agent_watchdog_limits()

        live_usage, live_state = self._live_worker_usage_callback(experiment_id, elog)
        implementation_result: CodexResult = worker.run(
            task=implementation_prompt,
            working_dir=worktree_path,
            sandbox="workspace-write",
            timeout_seconds=max_seconds,
            stall_seconds=stall_seconds,
            experiment_logger=elog,
            phase="implementation",
            on_process=lambda process: self._track_process(experiment_id, process),
            on_live_usage=live_usage,
        )
        implementation_usage_json = self._usage_after_run(
            experiment_id, implementation_result, live_state, elog
        )
        # Cancelled, stalled or stopped by the token budget (#533): keep that outcome and
        # start no fallback run.
        if self._ended_from_outside(experiment_id):
            return self.get_experiment(experiment_id)

        if (
            implementation_provider == "local"
            and (
                is_context_too_small(implementation_result.error)
                or getattr(worker, "local_attempt_passed_generated_tests", None) is False
            )
            and self.get_experiment(experiment_id).status != STATUS_CANCELLED
        ):
            # Context exhaustion means the local model was never called. Test-first
            # exhaustion means all bounded local correction rounds still failed the
            # generated acceptance checks. Both are explicit same-run handoff cases.
            generated_test_failure = getattr(worker, "local_attempt_passed_generated_tests", None) is False
            fallback_reason = "generated_tests_failed" if generated_test_failure else "context_too_small"
            generated_test_count = len(getattr(worker, "generated_tests", ()))
            model_written_tests = _take_model_written_tests(worker, worktree_path)
            if model_written_tests:
                # The paid coder (and any overload fallback below) gets the tests as
                # a suggestion; the reviewer reads the stored block (#636).
                implementation_prompt = with_model_written_tests(
                    implementation_prompt, model_written_tests
                )
                self._force_status(
                    experiment_id, STATUS_IMPLEMENTING,
                    model_written_tests=model_written_tests,
                )
            worker = self.worker_factory(worktree_path)
            implementation_provider = provider_of(worker)
            elog.warning(
                "implementation", "implementation_provider_fallback",
                f"local attempt requires paid fallback; implementation runs on "
                f"{implementation_provider or 'unknown'}: {implementation_result.error}",
                provider=implementation_provider or "",
                reason=fallback_reason,
                tests_generated=generated_test_count,
                tests_failing_on_base=generated_test_count,
                local_attempt_passed_generated_tests=False if generated_test_failure else None,
                paid_fallback_used=True,
                model_written_tests_handed_over=bool(model_written_tests),
            )
            self._force_status(
                experiment_id, STATUS_IMPLEMENTING,
                implementation_provider=implementation_provider,
            )
            live_usage, live_state = self._live_worker_usage_callback(experiment_id, elog)
            implementation_result = worker.run(
                task=implementation_prompt,
                working_dir=worktree_path,
                sandbox="workspace-write",
                timeout_seconds=max_seconds,
                stall_seconds=stall_seconds,
                experiment_logger=elog,
                phase="implementation",
                on_process=lambda process: self._track_process(experiment_id, process),
                on_live_usage=live_usage,
            )
            implementation_usage_json = self._usage_after_run(
                experiment_id, implementation_result, live_state, elog
            )
            if self._ended_from_outside(experiment_id):
                return self.get_experiment(experiment_id)

        fallback = (
            other_provider(implementation_provider)
            if implementation_provider and implementation_provider != "local"
            else None
        )
        if (
            implementation_result.error
            and not implementation_result.timed_out
            and is_provider_overload(implementation_result.error)
            and fallback is not None
            and self.get_experiment(experiment_id).status != STATUS_CANCELLED
        ):
            # Provider overloaded, not the work wrong: let the other coding agent try instead
            # of failing the experiment.
            elog.warning(
                "implementation", "implementation_provider_fallback",
                f"{implementation_provider} is overloaded; implementation runs on {fallback}",
                provider=fallback,
            )
            worker = build_worker(fallback, db_path=self.db_path)
            implementation_provider = fallback
            self._force_status(
                experiment_id, STATUS_IMPLEMENTING,
                implementation_provider=implementation_provider,
            )
            live_usage, live_state = self._live_worker_usage_callback(experiment_id, elog)
            implementation_result = worker.run(
                task=implementation_prompt,
                working_dir=worktree_path,
                sandbox="workspace-write",
                timeout_seconds=max_seconds,
                stall_seconds=stall_seconds,
                experiment_logger=elog,
                phase="implementation",
                on_process=lambda process: self._track_process(experiment_id, process),
                on_live_usage=live_usage,
            )
            implementation_usage_json = self._usage_after_run(
                experiment_id, implementation_result, live_state, elog
            )
            if self._ended_from_outside(experiment_id):
                return self.get_experiment(experiment_id)

        if self._ended_from_outside(experiment_id):
            return self.get_experiment(experiment_id)

        if implementation_result.timed_out:
            checkpoint_error = self._checkpoint_interrupted_work(
                experiment_id, worktree_path, github_work_tracking, elog
            )
            self._force_status(
                experiment_id, STATUS_TIMED_OUT,
                ended_at=_utcnow_iso(),
                implementation_ended_at=_utcnow_iso(),
                implementation_task_id=implementation_task_id,
                implementation_runtime_ms=implementation_result.runtime_ms,
                implementation_usage_json=implementation_usage_json,
                error="implementation phase timed out"
                + (f" ({implementation_result.error})" if implementation_result.error else "")
                + checkpoint_error,
            )
            return self.get_experiment(experiment_id)

        if implementation_result.error:
            checkpoint_error = self._checkpoint_interrupted_work(
                experiment_id, worktree_path, github_work_tracking, elog
            )
            self._force_status(
                experiment_id, STATUS_FAILED,
                ended_at=_utcnow_iso(),
                implementation_ended_at=_utcnow_iso(),
                implementation_task_id=implementation_task_id,
                implementation_runtime_ms=implementation_result.runtime_ms,
                implementation_usage_json=implementation_usage_json,
                error=implementation_result.error + checkpoint_error,
            )
            return self.get_experiment(experiment_id)

        self._transition(
            experiment_id, STATUS_IMPLEMENTING, STATUS_VERIFYING,
            implementation_ended_at=_utcnow_iso(),
            implementation_task_id=implementation_task_id,
            implementation_runtime_ms=implementation_result.runtime_ms,
            implementation_usage_json=implementation_usage_json,
        )
        _report_issue_progress(github_work_tracking, experiment_id, "verifying")

        if self._ended_from_outside(experiment_id):
            return self.get_experiment(experiment_id)

        elog.info("verifying", "verification_started", "Starting verification phase (git diff + tests)")
        result = self._verify(
            experiment_id,
            worktree_path,
            elog,
            github_work_tracking=github_work_tracking,
        )
        return self._repair_failed_verification(
            experiment_id, worktree_path, elog, result,
            github_work_tracking=github_work_tracking,
        )


    def _usage_after_run(
        self, experiment_id: str, run_result: Any, live_state: dict[str, int],
        elog: ExperimentLogger,
    ) -> str | None:
        """Account one finished worker run once (#532/#533).

        A run the live observer saw at least one API call of (Claude stream-json) is
        completed by :meth:`_finish_live_usage`: usage whose live write failed is added
        now, nothing committed live is counted twice, and enforce mode is applied
        again. Any other run (Codex, local, unstreamed) is counted here at its end,
        which is also where enforce mode can first stop it.
        """
        if live_state.get("calls") or live_state.get("pending_calls"):
            return self._finish_live_usage(experiment_id, run_result.usage, live_state, elog)
        return self._record_worker_run_usage(experiment_id, run_result.usage, elog)


    def _merge_current_main(self, worktree_path: str, elog: ExperimentLogger) -> str:
        """Bring today's main into a retry's issue branch (#478).

        A retry continues the issue branch, which can lag far behind main and conflict at
        adoption every time; merging main first lets the coding agent resolve a conflict
        once, in the experiment, where tests and review still check it. Returns a note for
        the prompt when conflict markers are left to resolve, else "".
        """
        def git(*args: str) -> subprocess.CompletedProcess:
            return _run_git(
                ["-c", "user.name=Nexus", "-c", "user.email=nexus@agent-lab.local", *args],
                cwd=worktree_path, timeout=120, elog=elog, phase="implementation",
            )

        if git("fetch", "--no-tags", "origin", "main").returncode != 0:
            return ""
        if git("merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD").returncode == 0:
            return ""
        if git("merge", "--no-edit", "FETCH_HEAD").returncode == 0:
            elog.info("implementation", "base_merged_clean", "Merged current main into the issue branch")
            return ""
        conflicted = git("diff", "--name-only", "--diff-filter=U").stdout.split()
        if not conflicted:
            git("merge", "--abort")
            return ""
        git("add", "-A")
        git("commit", "--no-verify", "-m", "Merge main; conflicts left for the coding agent")
        elog.warning(
            "implementation", "base_merge_conflict",
            f"Merging current main left conflicts in: {', '.join(conflicted[:20])}",
        )
        return (
            "FIRST: merging the current main into this branch left conflict markers "
            "(<<<<<<<, =======, >>>>>>>) in: " + ", ".join(conflicted[:20]) + ". Resolve "
            "them, keeping what both sides intended, then do the task below.\n\n"
        )


    def _repair_failed_verification(
        self, experiment_id: str, worktree_path: str, elog: ExperimentLogger,
        result: Experiment, *, github_work_tracking: Any | None = None,
    ) -> Experiment:
        """Give actionable test failures a small number of focused repair attempts."""
        if result.status != STATUS_FAILED or result.test_exit_code is None:
            return result
        # A local experiment is deliberately one model patch. A failed verification
        # is terminal and also activates the stable-task external fallback.
        if result.implementation_provider == "local":
            return result

        for repair_iteration in range(1, self.max_repair_iterations + 1):
            with self._lock:
                rows = self._conn.execute(
                    "SELECT failure_fingerprint FROM verification_runs "
                    "WHERE experiment_id = ? ORDER BY iteration",
                    (experiment_id,),
                ).fetchall()
                previous_fingerprints = [row[0] for row in rows[:-1] if row[0]]
            feedback = _truncate_text(
                f"Verification status: {result.status}\n"
                f"Exit code: {result.test_exit_code}\n"
                f"Failure: {result.error or 'tests failed'}\n"
                f"Output:\n{result.test_output or '(no output)'}",
                12000,
                label="verification feedback",
            )
            import hashlib
            current_fingerprint = hashlib.sha256(
                f"{result.status}\n{result.test_exit_code}\n{result.error or ''}\n"
                f"{result.test_output or ''}".encode()
            ).hexdigest()
            if current_fingerprint in previous_fingerprints:
                elog.warning(
                    "verifying", "repair_repeated_failure",
                    "Stopping repair loop because the verification failure repeated",
                    iteration=repair_iteration,
                )
                break

            worker = self.worker_factory(worktree_path)
            self._force_status(
                experiment_id, STATUS_VERIFYING,
                implementation_provider=provider_of(worker),
            )
            elog.warning(
                "implementing", "repair_started",
                f"Starting focused repair iteration {repair_iteration}/"
                f"{self.max_repair_iterations}",
                iteration=repair_iteration,
                provider=provider_of(worker) or "",
            )
            live_usage, live_state = self._live_worker_usage_callback(experiment_id, elog)
            repair_result = worker.run(
                # A repair coder is a paid coder too: it sees the same suggestion (#636).
                task=with_model_written_tests(
                    _build_repair_implementation_prompt(result.goal, feedback),
                    getattr(result, "model_written_tests", None),
                ),
                working_dir=worktree_path,
                sandbox="workspace-write",
                timeout_seconds=agent_watchdog_limits()[1],
                stall_seconds=agent_watchdog_limits()[0],
                experiment_logger=elog,
                phase="implementation_repair",
                on_process=lambda process: self._track_process(experiment_id, process),
                on_live_usage=live_usage,
            )
            self._usage_after_run(experiment_id, repair_result, live_state, elog)
            # Also a budget stop (#533): it must not become timed_out/failed or repair again.
            if self._ended_from_outside(experiment_id):
                return self.get_experiment(experiment_id)
            if repair_result.timed_out or repair_result.error:
                self._force_status(
                    experiment_id,
                    STATUS_TIMED_OUT if repair_result.timed_out else STATUS_FAILED,
                    ended_at=_utcnow_iso(),
                    error=repair_result.error or "repair phase timed out",
                )
                return self.get_experiment(experiment_id)

            result = self._verify(
                experiment_id, worktree_path, elog,
                github_work_tracking=github_work_tracking,
                repair_iteration=repair_iteration,
            )
            if result.status != STATUS_FAILED:
                return result

        final = self.get_experiment(experiment_id)
        if final.status == STATUS_FAILED:
            elog.error(
                "verifying", "repair_exhausted",
                "Verification remained failed after bounded repair attempts",
                max_repair_iterations=self.max_repair_iterations,
            )
        return final


    def _checkpoint_interrupted_work(
        self,
        experiment_id: str,
        worktree_path: str,
        github_work_tracking: Any | None,
        elog: ExperimentLogger,
    ) -> str:
        if github_work_tracking is None:
            return ""
        try:
            github_work_tracking.checkpoint_experiment(experiment_id, worktree_path)
            elog.info(
                "implementation", "github_checkpoint_published",
                "Published interrupted implementation checkpoint to Draft PR",
            )
            return ""
        except Exception as exc:  # noqa: BLE001 - preserve original terminal state
            elog.error(
                "implementation", "github_checkpoint_failed",
                "Could not publish interrupted implementation checkpoint", exc=exc,
            )
            return f"; GitHub checkpoint failed: {str(exc)[:1000]}"
