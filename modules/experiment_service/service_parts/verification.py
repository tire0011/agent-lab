"""Verification, CI and baseline-recheck methods of :class:`ExperimentService`."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from pathlib import Path
from typing import Any

from modules.central_logging import ExperimentLogger, sanitize_command
from modules.experiment_evaluation import parse_git_status_short
from modules.github_work_tracking import failing_test_names
from modules.run_store import get_setting, set_setting
from modules.test_run_log import global_heartbeat_bridge
from modules import timing_check as timing_check_module
from modules.verification_runner import (
    VerificationRunResult,
    WatchdogConfig,
    run_verification_test_monitored,
)

from ..git_utils import _run_git
from ..models import (
    Experiment,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_NEEDS_REVIEW,
    STATUS_SUCCEEDED,
    STATUS_TIMED_OUT,
)
from ..text_utils import _truncate_text, _utcnow_iso
from .core import (
    BASELINE_RECHECK_ENV_VAR,
    DEFAULT_BASELINE_RECHECK_SECONDS,
    _DEFAULT_TEST_COMMAND_KEY,
    _MAX_ERROR_TEST_NAMES,
    _TEST_COMMANDS,
    _TEST_TOTAL_SETTING_KEY,
    _baseline_verdict,
    _is_real_test_id,
    _test_file_for_id,
)


class VerificationMixin:
    """Verification, CI and baseline-recheck methods of :class:`ExperimentService`."""

    @staticmethod
    def _baseline_recheck_seconds() -> float:
        try:
            value = float(os.environ.get(BASELINE_RECHECK_ENV_VAR, ""))
        except ValueError:
            return DEFAULT_BASELINE_RECHECK_SECONDS
        return value if 0 < value < float("inf") else DEFAULT_BASELINE_RECHECK_SECONDS


    def _classify_failures_against_base(
        self,
        experiment_id: str,
        worktree_path: str,
        test_run: VerificationRunResult,
        elog: ExperimentLogger,
    ) -> dict[str, str] | None:
        """Tell pre-existing failures from regressions; None when not applicable.

        Everything that leaves the answer open returns None or ``regression``,
        which both keep today's outcome: a doubtful recheck must never excuse
        a change.
        """
        failed_ids = list(test_run.failed_test_ids or [])
        if test_run.exit_code == 0 or not failed_ids:
            return None
        if test_run.process_died or test_run.exception is not None:
            return None
        # Without the test that stalled, the abort itself stays unexplained
        # however the failures before it turn out.
        if test_run.timed_out and not test_run.aborted_test_id:
            return None
        base_commit = self.get_experiment(experiment_id).base_commit_sha
        if not base_commit:
            elog.info(
                "verifying", "baseline_recheck_skipped",
                "Experiment has no recorded base commit", reason="no_base_commit",
            )
            return None
        try:
            changed_files = self._files_changed_since(base_commit, worktree_path, elog)
            if {_test_file_for_id(test_id) for test_id in failed_ids} & changed_files:
                elog.info(
                    "verifying", "baseline_recheck_skipped",
                    "A failing test file is part of this experiment's change",
                    reason="test_file_changed",
                )
                return None
        except (OSError, subprocess.SubprocessError) as exc:
            elog.error(
                "verifying", "baseline_recheck_failed",
                "Could not determine this experiment's changed files", exc=exc,
            )
            return None
        return self._baseline_recheck(
            experiment_id, worktree_path, base_commit, failed_ids, elog,
        )


    @staticmethod
    def _files_changed_since(
        base_commit: str, worktree_path: str, elog: ExperimentLogger,
    ) -> set[str]:
        """Paths differing from *base_commit*, committed or not.

        ``git status`` alone would miss the work once a GitHub checkpoint has
        committed it.
        """
        changed: set[str] = set()
        for args in (
            ["diff", "--name-only", "--no-renames", base_commit],
            ["ls-files", "--others", "--exclude-standard"],
        ):
            proc = _run_git(args, cwd=worktree_path, elog=elog, phase="verifying")
            if proc.returncode != 0:
                raise subprocess.CalledProcessError(
                    proc.returncode, proc.args, output=proc.stdout, stderr=proc.stderr,
                )
            changed.update(line for line in proc.stdout.splitlines() if line)
        return changed


    def _baseline_recheck(
        self,
        experiment_id: str,
        worktree_path: str,
        base_commit: str,
        test_ids: list[str],
        elog: ExperimentLogger,
    ) -> dict[str, str]:
        """Run each implicated test once at the unchanged experiment base."""
        deadline = self._baseline_recheck_seconds()
        classifications = dict.fromkeys(test_ids, "regression")
        parent: str | None = None
        baseline_path: str | None = None
        try:
            parent = tempfile.mkdtemp(prefix="nexus-baseline-")
            baseline_path = str(Path(parent) / "worktree")
            proc = _run_git(
                ["worktree", "add", "--detach", baseline_path, base_commit],
                cwd=worktree_path, timeout=60, elog=elog, phase="verifying",
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or "git worktree add failed")
            config = WatchdogConfig(
                stall_timeout_seconds=deadline,
                check_interval_seconds=min(1.0, deadline),
                safety_cap_seconds=deadline,
            )
            for index, test_id in enumerate(test_ids):
                test_file = _test_file_for_id(test_id)
                # A loader error has no test to select, on base or anywhere.
                if test_file is None or not _is_real_test_id(test_id):
                    continue
                if self.get_experiment(experiment_id).status == STATUS_CANCELLED:
                    break
                # Same discovery root as the full run, so the IDs line up.
                command = [
                    self.test_python_executable, "-m", "unittest", "discover",
                    "-s", "tests", "-p", Path(test_file).name, "-k", test_id, "-v",
                ]
                result = run_verification_test_monitored(
                    command,
                    cwd=baseline_path,
                    run_id=f"{experiment_id}-baseline-{index}",
                    config=config,
                    on_heartbeat=global_heartbeat_bridge(elog, "verifying"),
                    prune_logs=False,
                    on_process=lambda process: self._track_process(experiment_id, process),
                )
                classifications[test_id] = _baseline_verdict(test_id, result)
        except Exception as exc:  # noqa: BLE001 - uncertain baseline is never exculpatory
            elog.error(
                "verifying", "baseline_recheck_failed",
                "Could not complete verification against the base commit", exc=exc,
            )
            classifications = dict.fromkeys(test_ids, "regression")
        finally:
            self._remove_baseline_worktree(worktree_path, parent, baseline_path, elog)

        elog.info(
            "verifying", "baseline_recheck_classified",
            "Classified failing tests against the experiment base commit",
            classifications=classifications,
            deadline_seconds=deadline,
            base_commit=base_commit,
        )
        return classifications


    @staticmethod
    def _remove_baseline_worktree(
        worktree_path: str, parent: str | None, baseline_path: str | None,
        elog: ExperimentLogger,
    ) -> None:
        try:
            if baseline_path is not None and Path(baseline_path).exists():
                _run_git(
                    ["worktree", "remove", "--force", baseline_path],
                    cwd=worktree_path, timeout=60, elog=elog, phase="verifying",
                )
        except (OSError, subprocess.SubprocessError) as exc:
            elog.error(
                "verifying", "baseline_worktree_remove_failed",
                "Could not remove the temporary base worktree", exc=exc,
            )
        if parent is not None:
            shutil.rmtree(parent, ignore_errors=True)
        try:
            # After the directory is gone, so this also clears what a failed
            # add or remove left behind in the shared worktree metadata.
            _run_git(
                ["worktree", "prune"], cwd=worktree_path, timeout=60,
                elog=elog, phase="verifying",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            elog.error(
                "verifying", "baseline_worktree_remove_failed",
                "Could not prune the temporary base worktree", exc=exc,
            )


    def _get_test_watchdog_config(self) -> WatchdogConfig:
        """Progress rules for the next local verification run.

        Constructor values win; anything unset falls through to the (clamped) environment
        and then to the watchdog's defaults.
        """
        return WatchdogConfig.resolve(
            stall_timeout_seconds=self.test_stall_timeout_seconds,
            check_interval_seconds=self.test_watchdog_interval_seconds,
            safety_cap_seconds=self.test_safety_cap_seconds,
        )


    def _get_expected_test_total(self) -> int | None:
        """How many tests the suite reported last time, if it ever finished.

        Only for readable progress ("842 / 1873") - a wrong or missing value costs nothing.
        """
        with self._lock:
            raw = get_setting(self._conn, _TEST_TOTAL_SETTING_KEY)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None


    def _remember_test_total(self, total: int | None) -> None:
        """Persist the total a completed run reported, for the next run."""
        if not total or total <= 0:
            return
        with self._lock:
            set_setting(
                self._conn,
                _TEST_TOTAL_SETTING_KEY,
                str(int(total)),
                updated_at=_utcnow_iso(),
            )


    def _record_verification_run(
        self, experiment_id: str, iteration: int, status: str,
        command: list[str] | None, exit_code: int | None, output: str | None,
        error: str | None, commit_sha: str | None = None,
    ) -> None:
        import hashlib

        fingerprint = hashlib.sha256(
            f"{status}\n{exit_code}\n{error or ''}\n{output or ''}".encode()
        ).hexdigest()
        now = _utcnow_iso()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO verification_runs
                   (experiment_id, iteration, started_at, ended_at, status,
                    commit_sha, command_json, exit_code, output, error,
                    failure_fingerprint)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (experiment_id, iteration, now, now, status, commit_sha,
                 json.dumps(command) if command else None, exit_code,
                 output, error, fingerprint),
            )


    def _timing_check_changed_paths(
        self, experiment_id: str, worktree_path: str, git_status: str,
        elog: ExperimentLogger,
    ) -> list[str]:
        """What this experiment changed: against its base, else the worktree status."""
        base_commit = self.get_experiment(experiment_id).base_commit_sha
        if base_commit:
            try:
                return sorted(self._files_changed_since(base_commit, worktree_path, elog))
            except (OSError, subprocess.SubprocessError):
                pass
        changed = parse_git_status_short(git_status)
        return [*changed.tracked_modified, *changed.untracked_new]


    def _forget_timing_process(self, experiment_id: str) -> None:
        """Best-effort: clear the tracked pytest handle after a failed timing check."""
        try:
            self._track_process(experiment_id, None)
        except Exception:  # noqa: BLE001 - cleanup must not reopen the gate
            pass


    def _run_timing_check(
        self, experiment_id: str, worktree_path: str, git_status: str,
        elog: ExperimentLogger,
    ) -> "timing_check_module.TimingCheckResult | None":
        """Slow-machine check (#681): rerun the affected tests with clock drift.

        Operational failures - pytest missing, collection errors, timeouts,
        crashes, and interruptions (KeyboardInterrupt/SystemExit) - become
        inconclusive results, which ``_verify`` records as ``needs_review``.
        An enabled check that could not run never approves a change, and an
        interruption never leaves the experiment stuck in ``verifying``.
        """
        if not timing_check_module.timing_check_enabled():
            elog.info("verifying", "timing_check_skipped",
                      "Timing check disabled", reason="disabled")
            return None
        try:
            changed = self._timing_check_changed_paths(
                experiment_id, worktree_path, git_status, elog,
            )
            affected = timing_check_module.select_affected_test_files(changed, worktree_path)
            if affected.discovery_errors:
                return timing_check_module.TimingCheckResult(
                    timing_check_module.STATUS_INCONCLUSIVE,
                    test_files=affected.files,
                    detail="affected-test discovery incomplete: "
                    + "; ".join(affected.discovery_errors[:10]),
                )
            if not affected.files:
                elog.info("verifying", "timing_check_skipped",
                          "No test file is affected by this change", reason="no_affected_tests")
                return None
            elog.info(
                "verifying", "timing_check_started",
                "Rerunning the affected tests with a sped-up clock",
                test_files=affected.files,
                drift_ms=timing_check_module.drift_from_env(),
            )
            result = timing_check_module.run_timing_check(
                self.test_python_executable, worktree_path, affected.files,
                on_process=lambda process: self._track_process(experiment_id, process),
            )
        except (KeyboardInterrupt, SystemExit) as exc:
            # The pytest child was killed by ``_run_pytest``; drop the tracked
            # handle in case the interruption hit before its own cleanup.
            self._forget_timing_process(experiment_id)
            elog.warning("verifying", "timing_check_inconclusive",
                         "Timing check was interrupted", reason=type(exc).__name__)
            return timing_check_module.TimingCheckResult(
                timing_check_module.STATUS_INCONCLUSIVE,
                detail=f"{timing_check_module.INTERRUPTED_DETAIL}: {type(exc).__name__}",
            )
        except Exception as exc:  # noqa: BLE001 - turn crashes into a closed gate
            self._forget_timing_process(experiment_id)
            elog.warning("verifying", "timing_check_inconclusive",
                         "Timing check could not run", reason=str(exc)[:1000])
            return timing_check_module.TimingCheckResult(
                timing_check_module.STATUS_INCONCLUSIVE,
                detail=f"timing check crashed: {exc}",
            )
        if result.status == timing_check_module.STATUS_INCONCLUSIVE:
            elog.warning("verifying", "timing_check_inconclusive",
                         "Timing check gave no verdict", reason=result.detail[:1000])
        elif result.status == timing_check_module.STATUS_PASSED:
            elog.info("verifying", "timing_check_passed",
                      "Affected tests also pass with a sped-up clock",
                      test_files=result.test_files, detail=result.detail)
        return result


    def _verify(
        self,
        experiment_id: str,
        worktree_path: str,
        elog: ExperimentLogger | None = None,
        *,
        github_work_tracking: Any | None = None,
        repair_iteration: int = 0,
    ) -> Experiment:
        if elog is None:
            elog = self.get_experiment_logger(experiment_id)
        if github_work_tracking is not None:
            try:
                ref = github_work_tracking.checkpoint_experiment(
                    experiment_id, worktree_path
                )
                elog.info(
                    "verifying", "github_checkpoint_published",
                    f"Checkpoint published to {ref.branch_name} before verification",
                )
            except Exception as exc:  # noqa: BLE001 - never verify invisible work
                elog.error(
                    "verifying", "github_checkpoint_failed",
                    "Verification blocked because the implementation checkpoint is not visible in GitHub",
                    exc=exc,
                )
                self._force_status(
                    experiment_id,
                    STATUS_NEEDS_REVIEW,
                    ended_at=_utcnow_iso(),
                    error=f"GitHub checkpoint failed before verification: {str(exc)[:2000]}",
                )
                return self.get_experiment(experiment_id)

        ci_verification = None
        if github_work_tracking is not None:
            elog.info(
                "verifying", "github_ci_wait_started",
                "Waiting for GitHub Actions verification of the exact checkpoint commit",
                checkpoint_sha=ref.checkpoint_sha,
            )
            try:
                ci_verification = github_work_tracking.wait_for_ci(experiment_id)
            except Exception as exc:  # noqa: BLE001 - local verification is the fallback
                elog.warning(
                    "verifying", "github_ci_unavailable",
                    "GitHub Actions verification lookup failed; using local fallback",
                    reason=str(exc)[:1000],
                )
            else:
                event = {
                    "passed": "github_ci_passed",
                    "failed": "github_ci_failed",
                    "unavailable": "github_ci_unavailable",
                }.get(ci_verification.status, "github_ci_unavailable")
                level = {
                    "passed": "info",
                    "failed": "error",
                }.get(ci_verification.status, "warning")
                getattr(elog, level)(
                    "verifying", event,
                    "GitHub Actions verification completed"
                    if ci_verification.status in {"passed", "failed"}
                    else "GitHub Actions verification unavailable; using local fallback",
                    checkpoint_sha=ci_verification.commit_sha,
                    conclusion=ci_verification.conclusion,
                    workflow_run_id=ci_verification.run_id,
                    workflow_url=ci_verification.url,
                    reason=ci_verification.reason,
                )
        try:
            status_proc = _run_git(
                ["status", "--short", "--untracked-files=all"],
                cwd=worktree_path, elog=elog, phase="verifying",
            )
            with tempfile.TemporaryDirectory(prefix="experiment-git-index-") as index_dir:
                index_env = dict(os.environ)
                index_env["GIT_INDEX_FILE"] = str(Path(index_dir) / "index")
                read_tree_proc = _run_git(
                    ["read-tree", "HEAD"], cwd=worktree_path, env=index_env, elog=elog, phase="verifying"
                )
                add_proc = _run_git(["add", "-A"], cwd=worktree_path, env=index_env, elog=elog, phase="verifying")
                if read_tree_proc.returncode != 0 or add_proc.returncode != 0:
                    failed_proc = read_tree_proc if read_tree_proc.returncode != 0 else add_proc
                    raise subprocess.CalledProcessError(
                        failed_proc.returncode,
                        failed_proc.args,
                        output=failed_proc.stdout,
                        stderr=failed_proc.stderr,
                    )
                stat_proc = _run_git(
                    ["diff", "--cached", "--stat", "HEAD"],
                    cwd=worktree_path,
                    env=index_env,
                    elog=elog, phase="verifying",
                )
                diff_proc = _run_git(
                    ["diff", "--cached", "HEAD"], cwd=worktree_path, env=index_env,
                    elog=elog, phase="verifying",
                )
                check_proc = _run_git(
                    ["diff", "--cached", "--check", "HEAD"],
                    cwd=worktree_path,
                    env=index_env,
                    elog=elog, phase="verifying",
                )
        except (OSError, subprocess.SubprocessError) as exc:
            elog.error(
                "verifying", "verification_git_exception",
                "Exception while running verification git commands", exc=exc,
            )
            self._force_status(
                experiment_id, STATUS_NEEDS_REVIEW,
                ended_at=_utcnow_iso(),
                error=f"verification git commands failed: {exc}",
            )
            return self.get_experiment(experiment_id)

        git_status = status_proc.stdout
        diff_stat = stat_proc.stdout
        diff_text = _truncate_text(diff_proc.stdout, self.max_diff_bytes, label="diff")
        diff_check_issue = check_proc.stdout.strip() or (check_proc.returncode != 0 and check_proc.stderr.strip())
        if git_status.strip():
            elog.debug("verifying", "git_status_checked", "Worktree has uncommitted changes as expected")
        else:
            elog.warning("verifying", "git_status_clean", "Worktree has no changes after implementation phase")

        test_command_key = _DEFAULT_TEST_COMMAND_KEY
        test_command_template = self.test_command or _TEST_COMMANDS.get(test_command_key)
        test_command = (
            [part.format(python=self.test_python_executable) for part in test_command_template]
            if test_command_template is not None
            else None
        )

        use_local_tests = ci_verification is None or ci_verification.status == "unavailable"
        if use_local_tests and (test_command is None or not (Path(worktree_path) / "tests").is_dir()):
            elog.warning(
                "verifying", "no_test_command",
                "No safe test command could be determined for this worktree",
            )
            self._force_status(
                experiment_id, STATUS_NEEDS_REVIEW,
                ended_at=_utcnow_iso(),
                git_status=git_status,
                diff_stat=diff_stat,
                diff_text=diff_text,
                error="no safe test command could be determined for this worktree",
            )
            return self.get_experiment(experiment_id)

        test_timed_out = False
        baseline_classifications: dict[str, str] | None = None
        ci_failure_detail = None
        if not use_local_tests:
            test_command = ["GitHub Actions", "tests.yml", ci_verification.commit_sha]
            test_exit_code = 0 if ci_verification.status == "passed" else 1
            test_output = (
                f"GitHub Actions run {ci_verification.run_id or 'unknown'} "
                f"for commit {ci_verification.commit_sha} completed with conclusion "
                f"{ci_verification.conclusion or 'unknown'}."
                + (f" {ci_verification.url}" if ci_verification.url else "")
            )
            # The first line stays as it is: ci_evidence_backfill parses it.
            ci_failure_detail = getattr(ci_verification, "failure_detail", None)
            if ci_failure_detail:
                test_output = f"{test_output}\n{ci_failure_detail}"
        else:
            watchdog = self._get_test_watchdog_config()
            expected_total = self._get_expected_test_total()
            elog.info(
                "verifying", "local_tests_fallback_started",
                "Running local verification because matching GitHub CI was unavailable",
                command=test_command, cwd=worktree_path,
                stall_timeout_seconds=watchdog.stall_timeout_seconds,
                safety_cap_seconds=watchdog.safety_cap_seconds,
            )
            elog.info(
                "verifying", "tests_started",
                "Running verification test command under the progress watchdog",
                command=test_command, cwd=worktree_path,
                stall_timeout_seconds=watchdog.stall_timeout_seconds,
                watchdog_interval_seconds=watchdog.check_interval_seconds,
                safety_cap_seconds=watchdog.safety_cap_seconds,
                expected_total_tests=expected_total,
            )
            test_run = run_verification_test_monitored(
                test_command,
                cwd=worktree_path,
                run_id=experiment_id,
                config=watchdog,
                expected_total=expected_total,
                # Publishing the test heartbeat into this experiment's log keeps the global Nexus
                # heartbeat alive during a long suite; per-test entries stay in the dedicated test log.
                on_heartbeat=global_heartbeat_bridge(elog, "verifying"),
                on_process=lambda process: self._track_process(experiment_id, process),
            )
            if self._ended_from_outside(experiment_id):
                return self.get_experiment(experiment_id)
            test_exit_code = test_run.exit_code
            test_output = test_run.output
            test_timed_out = test_run.timed_out
            self._remember_test_total(test_run.tests_total)

            baseline_classifications = self._classify_failures_against_base(
                experiment_id, worktree_path, test_run, elog,
            )
            if self.get_experiment(experiment_id).status == STATUS_CANCELLED:
                return self.get_experiment(experiment_id)

        if use_local_tests and test_run.timed_out:
            elog.log_process(
                "verifying",
                "test_stalled" if test_run.stalled else "test_safety_cap_exceeded",
                test_run.stall_diagnostics or "Test run aborted by the progress watchdog",
                command=test_command, cwd=worktree_path,
                duration_ms=test_run.duration_ms,
                timed_out=True, level="ERROR",
                stall_timeout_seconds=watchdog.stall_timeout_seconds,
                safety_cap_seconds=watchdog.safety_cap_seconds,
                test_log_path=test_run.test_log_path,
                **(test_run.progress or {}),
            )
        elif use_local_tests and test_run.process_died:
            elog.log_process(
                "verifying", "test_process_died",
                test_run.stall_diagnostics or "Test process was killed before it could finish",
                command=test_command, cwd=worktree_path,
                duration_ms=test_run.duration_ms,
                level="ERROR",
                test_log_path=test_run.test_log_path,
                **(test_run.progress or {}),
            )
        elif use_local_tests and test_run.exception is not None:
            elog.error(
                "verifying", "tests_exception",
                "Exception while running test command", exc=test_run.exception,
                command=sanitize_command(test_command), cwd=worktree_path,
            )
        elif use_local_tests:
            elog.log_process(
                "verifying", "tests_completed",
                f"Test command finished with exit code {test_exit_code}",
                command=test_command, cwd=worktree_path,
                duration_ms=test_run.duration_ms,
                exit_code=test_exit_code, stdout=test_run.stdout, stderr=test_run.stderr,
                test_log_path=test_run.test_log_path,
                tests_completed=(test_run.progress or {}).get("tests_completed"),
                tests_total=test_run.tests_total,
            )

        test_output_capped = _truncate_text(test_output, self.max_test_output_bytes, label="test output")

        final_status = STATUS_SUCCEEDED
        error: str | None = None
        quality_evaluation: dict[str, Any] | None = None
        all_failures_pre_existing = bool(baseline_classifications) and all(
            value == "pre_existing" for value in baseline_classifications.values()
        )
        tests_green = (
            test_exit_code == 0
            and not test_timed_out
            and not (use_local_tests and test_run.process_died)
            and not all_failures_pre_existing
            and not diff_check_issue
        )
        timing_result = (
            self._run_timing_check(experiment_id, worktree_path, git_status, elog)
            if tests_green else None
        )
        if self._ended_from_outside(experiment_id):
            return self.get_experiment(experiment_id)
        if all_failures_pre_existing:
            final_status = STATUS_NEEDS_REVIEW
            error = "pre-existing failures on base: " + ", ".join(
                sorted(baseline_classifications)
            )
            if diff_check_issue:
                error += f"; git diff --check reported issues: {str(diff_check_issue)[:500]}"
            elog.warning("verifying", "baseline_pre_existing_failures", error)
        elif diff_check_issue:
            final_status = STATUS_NEEDS_REVIEW
            error = f"git diff --check reported issues: {str(diff_check_issue)[:500]}"
            elog.warning("verifying", "diff_check_issue", f"git diff --check reported issues: {diff_check_issue}")
        elif test_timed_out:
            # An aborted test run is not a test failure (exit_code=None is expected): keep this
            # branch ahead of the exit-code check so it can never fold into "tests failed". The
            # distinct "test_stalled"/"test_safety_cap_exceeded" event was already logged above.
            final_status = STATUS_TIMED_OUT
            if use_local_tests and test_run.stalled:
                error = (
                    "tests stalled (no exit code, no test started or finished for "
                    f"{int(watchdog.stall_timeout_seconds)}s)"
                )
            elif use_local_tests and test_run.safety_cap_exceeded:
                error = (
                    "tests hit the absolute safety cap (no exit code, run exceeded "
                    f"{int(watchdog.safety_cap_seconds)}s)"
                )
            else:
                error = "tests timed out (no exit code, test command did not finish within the time limit)"
            if use_local_tests and test_run.stall_diagnostics:
                error = f"{error}\n{test_run.stall_diagnostics}"
        elif use_local_tests and test_run.process_died:
            # Killed by a signal (OOM, an operator, the host): no verdict was reached, so it must
            # not be filed as a test failure.
            final_status = STATUS_NEEDS_REVIEW
            error = "test process died before finishing (killed by a signal, no exit code)"
            if test_run.stall_diagnostics:
                error = f"{error}\n{test_run.stall_diagnostics}"
        elif test_exit_code != 0:
            final_status = STATUS_FAILED if test_exit_code is not None else STATUS_NEEDS_REVIEW
            error = f"tests failed (exit code {test_exit_code})"
            failing_tests = failing_test_names(ci_failure_detail, limit=_MAX_ERROR_TEST_NAMES)
            if failing_tests:
                error = f"{error}: {', '.join(failing_tests)}"
            elog.error(
                "verifying",
                "tests_failed" if repair_iteration == 0 else "tests_failed_repair",
                error,
                exit_code=test_exit_code,
                iteration=repair_iteration,
            )
        elif timing_result is not None and timing_result.failed:
            # Green here, red on a slower machine (#681): a failed verification,
            # so the repair loop and the retry task get the hint in ``error``.
            final_status = STATUS_FAILED
            error = timing_result.error_message()
            test_output_capped = _truncate_text(
                f"{test_output_capped}\n\n--- timing check "
                f"({timing_check_module.CLOCK_DRIFT_ENV_VAR}={timing_result.drift_ms:g}) ---\n"
                f"{timing_result.output}",
                self.max_test_output_bytes, label="test output",
            )
            elog.error(
                "verifying", "timing_sensitive_tests", error,
                timing_sensitive=timing_result.timing_sensitive,
                test_files=timing_result.test_files,
                drift_ms=timing_result.drift_ms,
                iteration=repair_iteration,
            )
        elif (timing_result is not None
              and timing_result.status == timing_check_module.STATUS_INCONCLUSIVE):
            final_status = STATUS_NEEDS_REVIEW
            error = f"timing check inconclusive: {timing_result.detail or 'no verdict'}"
            if timing_result.output:
                test_output_capped = _truncate_text(
                    f"{test_output_capped}\n\n--- timing check incomplete ---\n"
                    f"{timing_result.output}",
                    self.max_test_output_bytes, label="test output",
                )
            elog.error(
                "verifying", "timing_check_failed_closed", error,
                test_files=timing_result.test_files,
                iteration=repair_iteration,
            )
        else:
            elog.info("verifying", "tests_passed", "Verification tests passed")

            # Deterministic quality evaluation (no LLM call): tests passing is necessary but never
            # sufficient - the diff must match what plan and task required. Any failure in this
            # block downgrades to needs_review with the worktree preserved, never a silent "succeeded".
            try:
                experiment_for_eval = self.get_experiment(experiment_id)
                changed = parse_git_status_short(git_status)
                import modules.experiment_service as experiment_service_module

                eval_result = experiment_service_module.evaluate_implementation(
                    experiment_for_eval.plan,
                    experiment_for_eval.goal,
                    changed,
                    worktree_path,
                )
                quality_evaluation = eval_result.to_dict()
                # Only artifact-evidence checks failed and tests are green: the reviewer decides at
                # adoption (#519); every non-GitHub-tracked run keeps the hard stop.
                review_findings = (
                    eval_result.soft_findings() if github_work_tracking is not None else None
                )
                if review_findings is not None:
                    quality_evaluation["outcome"] = "pass"
                    quality_evaluation["review_findings"] = review_findings
                    elog.warning(
                        "verifying", "quality_evaluation_to_reviewer",
                        f"Quality gate findings go to the reviewer: {review_findings}",
                        evaluation_json=json.dumps(quality_evaluation),
                    )
                elif eval_result.outcome != "pass":
                    final_status = STATUS_NEEDS_REVIEW
                    error = f"quality evaluation: {eval_result.reason}"
                    elog.warning(
                        "verifying", "quality_evaluation_failed",
                        f"Deterministic quality evaluation flagged this experiment for review: {eval_result.reason}",
                        evaluation_json=json.dumps(quality_evaluation),
                    )
                else:
                    elog.info(
                        "verifying", "quality_evaluation_passed",
                        "Deterministic quality evaluation passed",
                        evaluation_json=json.dumps(quality_evaluation),
                    )
            except Exception as exc:  # noqa: BLE001 - never let this promote to succeeded
                final_status = STATUS_NEEDS_REVIEW
                error = f"quality evaluation failed unexpectedly: {exc}"
                elog.error(
                    "verifying", "quality_evaluation_exception",
                    "Exception while running deterministic quality evaluation", exc=exc,
                )

        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(iteration), -1) + 1 FROM verification_runs "
                "WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
        self._record_verification_run(
            experiment_id,
            int(row[0]),
            final_status,
            test_command,
            test_exit_code,
            test_output_capped,
            error,
            (ci_verification.commit_sha if ci_verification is not None else None),
        )
        self._force_status(
            experiment_id, final_status,
            ended_at=_utcnow_iso(),
            git_status=git_status,
            diff_stat=diff_stat,
            diff_text=diff_text,
            test_command_json=json.dumps(test_command),
            test_exit_code=test_exit_code,
            test_output=test_output_capped,
            error=error,
            quality_evaluation_json=json.dumps(quality_evaluation) if quality_evaluation is not None else None,
            baseline_test_classifications_json=(
                json.dumps(baseline_classifications)
                if baseline_classifications is not None else None
            ),
        )
        if github_work_tracking is not None:
            try:
                github_work_tracking.record_verification(
                    experiment_id,
                    status=final_status,
                    command=test_command,
                    exit_code=test_exit_code,
                    error=error,
                    output=test_output_capped,
                )
            except Exception as exc:  # noqa: BLE001 - keep work and require review
                elog.error(
                    "verifying", "github_verification_update_failed",
                    "Verification completed but the Draft PR diagnostics could not be updated",
                    exc=exc,
                )
                current = self.get_experiment(experiment_id)
                combined_error = (
                    f"{current.error}; " if current.error else ""
                ) + f"GitHub verification update failed: {str(exc)[:1000]}"
                self._force_status(
                    experiment_id,
                    STATUS_NEEDS_REVIEW,
                    ended_at=_utcnow_iso(),
                    error=combined_error,
                )
        return self.get_experiment(experiment_id)
