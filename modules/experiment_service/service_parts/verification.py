"""Verification, CI and baseline-recheck methods of :class:`ExperimentService`."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger("modules.experiment_service")

from modules.central_logging import ExperimentLogger, sanitize_command
from modules.experiment_evaluation import parse_git_status_short
from modules.github_work_tracking import failing_test_names
from modules.review_size_stats import DiffSize, parse_numstat
from modules.run_store import get_setting, set_setting
from modules.test_run_log import global_heartbeat_bridge
from modules import timing_check as timing_check_module
from modules.verification_runner import (
    HungTest,
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
    STATUS_VERIFYING,
)
from ..text_utils import _truncate_text, _utcnow_iso
from ..whitespace_fix import diff_check_report, fix_whitespace
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


# Slow-machine check (#681/#689): on unless explicitly "0".
TIMING_CHECK_ENV_VAR = "NEXUS_TIMING_CHECK"
TIMING_CHECK_HINT = (
    "inject the clock or compute the value from configured values, not from wall time"
)
# #725: every flagged test fails under the same drift on the base commit too.
TIMING_VERDICT_KNOWN_DRIFT = "passed_with_known_drift"


def timing_check_enabled() -> bool:
    return os.environ.get(TIMING_CHECK_ENV_VAR, "1").strip() != "0"


# #711: one local repair attempt for a drift-only timing-check failure.
TIMING_LOCAL_REPAIR_ENV_VAR = "NEXUS_TIMING_LOCAL_REPAIR"
TIMING_LOCAL_REPAIR_MAX_ROUNDS = 2
TIMING_LOCAL_REPAIR_COMMIT_MESSAGE = "timing-check local repair"
TIMING_LOCAL_REPAIR_BUDGET_SECONDS = 900.0
TIMING_LOCAL_REPAIR_PHASE = "timing_local_repair"
_TIMING_REPAIR_MAX_LIKELY_FILES = 8
_TIMING_REPAIR_FEEDBACK_CHARS = 4000
# A repair that skips or xfails a flagged test only hides the problem.
_TIMING_REPAIR_SKIP = re.compile(r"^\+.*\b(?:skip|skipIf|skipUnless|xfail)\b", re.MULTILINE)
# A candidate that detects or disables the injected drift would make the drift
# rerun pass vacuously, so it is rejected like a skip.
_TIMING_REPAIR_DRIFT_TAMPER = re.compile(r"^\+.*NEXUS_TEST_CLOCK_DRIFT_MS", re.MULTILINE)


class _TimingRepairRejected(Exception):
    """A candidate that cannot be committed as only its own delta."""


def timing_local_repair_enabled() -> bool:
    return os.environ.get(TIMING_LOCAL_REPAIR_ENV_VAR, "1").strip().lower() not in {
        "0", "false", "off", "no",
    }


def _no_free_tier(_prompt: str) -> None:
    """Planner fallback that never calls a model: the repair is local-only."""
    return None


def _default_timing_repair_worker(likely_files: tuple[str, ...]) -> Any:
    """A local-only worker: no task split, no test-first, no free-tier planner."""
    from modules.llama_cpp_client import LlamaCppClient
    from modules.local_coding_worker import LocalCodingWorker

    return LocalCodingWorker(
        client=LlamaCppClient(timeout=300), split_tasks=False, test_first=False,
        planner=_no_free_tier, patch_rounds=1, likely_files=likely_files,
    )


def _is_generated_path(path: str) -> bool:
    """Bytecode a test run writes is never part of a repair."""
    return "__pycache__/" in f"/{path}" or path.endswith((".pyc", ".pyo"))


# Pytest loads conftest.py files and these root config files; a repair that
# changes them (e.g. ``--noconftest`` in addopts) could silence the drift
# fixture, so they are protected even when they do not name the marker.
_TIMING_REPAIR_CONFIG_NAMES = {"conftest.py"}
_TIMING_REPAIR_ROOT_CONFIGS = {"pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"}


def _timing_repair_protected_paths(
    worktree_path: str, elog: ExperimentLogger,
) -> tuple[set[str], set[str]] | None:
    """``(protected, marker_files)`` a repair must never change, or None on scan failure.

    ``protected`` is every file carrying the CLOCK_DRIFT marker (the drift
    fixture in ``tests/conftest.py`` and everything else naming
    ``NEXUS_TEST_CLOCK_DRIFT_MS``) plus every ``conftest.py`` and root pytest
    config file. ``marker_files`` is the subset whose marker must stay present.
    A candidate that deletes or neuters any of them would make the drift rerun
    pass vacuously, so it is rejected instead of judged.
    """
    marker = timing_check_module.CLOCK_DRIFT_ENV
    paths: list[str] = []
    for args in (
        ["ls-files", "-z", "--cached"],
        ["ls-files", "-z", "--others", "--exclude-standard"],
    ):
        proc = _run_git(args, cwd=worktree_path, elog=elog, phase=TIMING_LOCAL_REPAIR_PHASE)
        if proc.returncode != 0:
            return None
        paths.extend(path for path in proc.stdout.split("\0") if path)
    root = Path(worktree_path)
    protected: set[str] = set()
    marker_files: set[str] = set()
    for path in dict.fromkeys(paths):
        if _is_generated_path(path):
            continue
        pure = PurePosixPath(path)
        if pure.name in _TIMING_REPAIR_CONFIG_NAMES or path in _TIMING_REPAIR_ROOT_CONFIGS:
            protected.add(path)
        try:
            file = root / path
            if file.is_file() and marker in file.read_text(encoding="utf-8", errors="ignore"):
                marker_files.add(path)
        except OSError:
            continue
    return protected | marker_files, marker_files


def _drift_fixture_broken(worktree_path: str, marker_files: set[str]) -> list[str]:
    """Marker files that are gone or no longer carry the CLOCK_DRIFT marker."""
    marker = timing_check_module.CLOCK_DRIFT_ENV
    broken: list[str] = []
    for path in sorted(marker_files):
        try:
            content = (Path(worktree_path) / path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            broken.append(path)
            continue
        if marker not in content:
            broken.append(path)
    return broken


@dataclass(slots=True)
class TimingLocalRepair:
    """A committed local repair (#711): what verification adds to its record."""

    commit_sha: str
    paths: list[str]
    diff: str
    stat: str


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


    def _run_timing_check(
        self, experiment_id: str, worktree_path: str, head_diff: str,
        elog: ExperimentLogger, *, watchdog: WatchdogConfig | None = None,
        github_work_tracking: Any | None = None,
    ) -> tuple[str, str] | None:
        """Slow-machine check (#689): rerun the affected tests with clock drift.

        Returns None when the change may proceed, else ``(status, error)``.
        A flagged test fails verification with a hint for the retry; anything
        without a verdict (a check error, a crash, an interruption) is
        ``needs_review`` - an enabled check that could not run never approves.
        A flagged test that fails under the same drift on the base commit too
        (#725) is known drift, not this change's fault: when all flagged tests
        are, the verdict is ``passed_with_known_drift`` and the change proceeds.
        """
        drift_env: dict[str, str] | None = None
        if not timing_check_enabled():
            elog.info(
                "verifying", "timing_check_skipped",
                f"Slow-machine check disabled by {TIMING_CHECK_ENV_VAR}=0",
            )
            return None
        # Per-test failure text by exact node id (#737), for the events below.
        drift_tails: dict[str, str] = {}
        control_tails: dict[str, str] = {}
        tests: list[str] = []
        try:
            # The HEAD diff alone misses work a GitHub checkpoint already
            # committed, so the paths changed since the base count too.
            base_commit = self.get_experiment(experiment_id).base_commit_sha
            paths = (
                self._files_changed_since(base_commit, worktree_path, elog)
                if base_commit else set()
            )
            diff = head_diff + "\n" + "".join(
                f"diff --git a/{path} b/{path}\n" for path in sorted(paths)
            )
            tests = timing_check_module.affected_tests(diff, worktree_path)
            elog.info(
                "verifying", "timing_check_started",
                "Rerunning the affected tests with a sped-up clock",
                test_files=tests,
            )

            def track(process: subprocess.Popen[str] | None) -> None:
                self._track_process(experiment_id, process)

            options = {"watchdog": watchdog, "run_id": f"{experiment_id}-timing"} if watchdog else {}
            # One drift environment for the branch run and the base rerun (#725).
            drift_env = timing_check_module.drift_env()
            result = timing_check_module.run_timing_check(
                worktree_path, tests, on_process=track, python=self.test_python_executable,
                tails=drift_tails, env=drift_env, **options,
            )
            control = None
            flagged = timing_check_module.flagged_tests(result)
            if flagged:
                # The normal run may have been GitHub CI, so a flag only counts
                # when the same test passes here without drift.
                if watchdog is not None:
                    options["run_id"] = f"{experiment_id}-timing-control"
                control = timing_check_module.control_check(
                    worktree_path, flagged, on_process=track,
                    python=self.test_python_executable, tails=control_tails, **options,
                )
                if control is None:
                    result = "error: control run without drift gave no verdict"
        except HungTest:
            raise
        except (KeyboardInterrupt, SystemExit) as exc:
            result, control = f"error: interrupted ({type(exc).__name__})", None
        except Exception as exc:  # noqa: BLE001 - a crashed check is no verdict
            result, control = f"error: {type(exc).__name__}: {str(exc)[:500]}", None

        if result == "ok":
            elog.info("verifying", "timing_check_passed", "Slow-machine check passed")
            return None
        if control is not None:
            drift_only, fails_without_drift = control
            if drift_only:
                base = self._timing_base_drift(
                    experiment_id, worktree_path, drift_only, drift_env, drift_tails, elog,
                    watchdog=watchdog, github_work_tracking=github_work_tracking,
                )
                if base is not None:
                    regressed, base_drift = base
                    if not regressed:
                        elog.warning(
                            "verifying", "timing_check_passed_with_known_drift",
                            "Every flagged test fails under the same drift on the base "
                            "commit too; the change did not cause it",
                            verdict=TIMING_VERDICT_KNOWN_DRIFT, known_drift_tests=base_drift,
                            tails={t: drift_tails[t] for t in base_drift if t in drift_tails},
                        )
                        return None
                    drift_only = regressed
                error = (
                    f"{timing_check_module.TIMING_SENSITIVE_PREFIX}{', '.join(drift_only)}"
                    f"; hint: {TIMING_CHECK_HINT}"
                )
                elog.error(
                    "verifying", "timing_check_flagged", error,
                    fails_without_drift=fails_without_drift,
                    tails={test: drift_tails[test] for test in drift_only if test in drift_tails},
                )
                # #711: only drift-only regressions reach here (no host failures,
                # no known base drift), so a local fix may still save the run.
                if drift_env is not None and timing_local_repair_enabled():
                    repair = self._timing_local_repair(
                        experiment_id, worktree_path, drift_only, drift_env, drift_tails,
                        tests, elog, watchdog=watchdog, host_failures=fails_without_drift,
                    )
                    if repair is not None:
                        if not hasattr(self, "_timing_local_repairs"):
                            self._timing_local_repairs = {}
                        self._timing_local_repairs[experiment_id] = repair
                        return None
                return STATUS_FAILED, error
            result = (
                "error: these tests fail on this host without drift too, so there is "
                "no timing verdict: " + ", ".join(fails_without_drift)
            )
            # Why they fail here: the control run's text, else the drift run's.
            tails = {
                test: control_tails.get(test) or drift_tails[test]
                for test in fails_without_drift
                if test in control_tails or test in drift_tails
            }
        else:
            tails = dict(drift_tails)
        error = f"timing check failed closed: {result}"
        elog.warning("verifying", "timing_check_failed_closed", error, tails=tails)
        return STATUS_NEEDS_REVIEW, error


    def _timing_local_repair(
        self, experiment_id: str, worktree_path: str, flagged: list[str],
        drift_env: dict[str, str], drift_tails: dict[str, str], affected: list[str],
        elog: ExperimentLogger, *, watchdog: WatchdogConfig | None = None,
        host_failures: list[str] | tuple[str, ...] = (),
    ) -> TimingLocalRepair | None:
        """Let the local worker fix drift-only tests (#711); None keeps today's verdict.

        At most two rounds. Each candidate is judged by the timing check itself
        (same ids, same drift environment) plus a run of the affected test
        files without drift. A passing candidate is committed onto the branch
        as ``timing-check local repair``; anything else is undone so the
        worktree is exactly as it was before the attempt.
        """
        started = time.monotonic()
        rounds = 0
        tokens = 0
        drift_ms = drift_env.get(timing_check_module.CLOCK_DRIFT_ENV, "")
        try:
            before = self._worktree_tree(worktree_path, elog)
        except (OSError, subprocess.SubprocessError) as exc:
            elog.error(
                TIMING_LOCAL_REPAIR_PHASE, "timing_check_local_repair_failed",
                "Could not snapshot the worktree before the local repair", exc=exc,
                rounds=0, tokens=0, seconds=round(time.monotonic() - started, 3),
            )
            return None
        # The drift fixture and pytest's loading of it must survive every
        # candidate untouched; a repair that neutered them would make the
        # drift rerun pass vacuously.
        scanned = _timing_repair_protected_paths(worktree_path, elog)
        if scanned is None:
            elog.error(
                TIMING_LOCAL_REPAIR_PHASE, "timing_check_local_repair_failed",
                "Could not scan the worktree for the timing-drift fixture; no repair",
                rounds=0, tokens=0, seconds=round(time.monotonic() - started, 3),
            )
            return None
        protected, marker_files = scanned
        try:
            base_commit = self.get_experiment(experiment_id).base_commit_sha
            changed = sorted(
                self._files_changed_since(base_commit, worktree_path, elog)
                if base_commit else set()
            )
        except (OSError, subprocess.SubprocessError):
            changed = []
        test_files = [f for f in dict.fromkeys(t.split("::", 1)[0] for t in flagged) if f]
        likely = tuple(
            path for path in dict.fromkeys([*test_files, *changed])
            if not _is_generated_path(path) and (Path(worktree_path) / path).is_file()
        )[:_TIMING_REPAIR_MAX_LIKELY_FILES]
        task = self._timing_repair_task(flagged, drift_tails, drift_ms, changed, likely)
        elog.info(
            TIMING_LOCAL_REPAIR_PHASE, "timing_check_local_repair_started",
            "Asking the local coding worker to make drift-only tests clock-independent",
            tests=flagged, drift_ms=drift_ms, changed_files=changed,
            max_rounds=TIMING_LOCAL_REPAIR_MAX_ROUNDS,
        )
        factory = getattr(self, "timing_repair_worker_factory", None)
        failure = ""
        previous_patch = ""
        repair: TimingLocalRepair | None = None
        try:
            worker = (
                factory(worktree_path) if factory is not None
                else _default_timing_repair_worker(likely)
            )
            for round_no in range(1, TIMING_LOCAL_REPAIR_MAX_ROUNDS + 1):
                remaining = TIMING_LOCAL_REPAIR_BUDGET_SECONDS - (time.monotonic() - started)
                if remaining <= 0:
                    failure = failure or "local repair ran out of time"
                    break
                if self.get_experiment(experiment_id).status == STATUS_CANCELLED:
                    failure = "experiment was cancelled"
                    break
                rounds = round_no
                feedback = (
                    f"\n\nPrevious local round failed:\n{failure[-_TIMING_REPAIR_FEEDBACK_CHARS:]}\n"
                    f"Previous patch (discarded):\n{previous_patch[:_TIMING_REPAIR_FEEDBACK_CHARS]}"
                    if failure else ""
                )
                result = worker.run(
                    task=task + feedback, working_dir=worktree_path, sandbox="workspace-write",
                    timeout_seconds=remaining, experiment_logger=elog,
                    phase=TIMING_LOCAL_REPAIR_PHASE,
                )
                tokens += self._timing_repair_tokens(worker, result)
                after = self._worktree_tree(worktree_path, elog)
                paths = [
                    path for path in self._tree_diff_names(before, after, worktree_path, elog)
                    if not _is_generated_path(path)
                ]
                patch = self._tree_diff(before, after, paths, worktree_path, elog) if paths else ""
                if not getattr(result, "success", False):
                    failure = getattr(result, "error", None) or "the local worker gave no patch"
                elif not paths:
                    failure = "the local worker made no change"
                elif _TIMING_REPAIR_SKIP.search(patch):
                    failure = "the patch skips or xfails a test instead of fixing it"
                elif _TIMING_REPAIR_DRIFT_TAMPER.search(patch):
                    failure = (
                        "the patch references the timing-drift marker "
                        f"({timing_check_module.CLOCK_DRIFT_ENV}) to detect or disable "
                        "the injected drift"
                    )
                else:
                    touched = sorted(set(paths) & protected)
                    broken = _drift_fixture_broken(worktree_path, marker_files)
                    if touched or broken:
                        failure = "the patch touches the timing-drift fixture or pytest config" + (
                            f": {', '.join(touched)}" if touched else ""
                        ) + (
                            f"; the marker is gone from: {', '.join(broken)}" if broken else ""
                        )
                    else:
                        failure = self._judge_timing_repair(
                            experiment_id, worktree_path, flagged, drift_env, affected, paths,
                            elog, watchdog=watchdog, host_failures=host_failures,
                            protected=protected, marker_files=marker_files,
                        )
                        if not failure:
                            repair = self._commit_timing_repair(
                                worktree_path, before, paths, patch, elog,
                            )
                            break
                previous_patch = patch
                self._restore_worktree_tree(worktree_path, before, elog)
        except _TimingRepairRejected as exc:
            failure = str(exc)
        except HungTest as exc:
            failure = f"a repaired test hung: {exc}"
        except (KeyboardInterrupt, SystemExit) as exc:
            # Like an interruption of the check itself: the worktree and the
            # branch are put back and the verdict falls back to today's.
            failure = f"interrupted ({type(exc).__name__})"
        except Exception as exc:  # noqa: BLE001 - a broken repair keeps today's verdict
            failure = f"{type(exc).__name__}: {str(exc)[:500]}"
        if repair is not None:
            # The commit is on the branch and the worktree matches it; nothing
            # is restored, so verification continues with the repair applied.
            elog.info(
                TIMING_LOCAL_REPAIR_PHASE, "timing_check_local_repair_passed",
                f"Local repair fixed the drift-only tests in {rounds} round(s); "
                f"committed {repair.commit_sha[:12]}",
                rounds=rounds, tokens=tokens,
                seconds=round(time.monotonic() - started, 3),
                commit_sha=repair.commit_sha, paths=repair.paths, tests=flagged,
                paid_retry_avoided=True,
            )
            return repair
        try:
            self._restore_worktree_tree(worktree_path, before, elog)
        except Exception as exc:  # noqa: BLE001 - reported; the verdict stays a failure
            failure = f"{failure}; restoring the worktree failed: {exc}"
        elog.warning(
            TIMING_LOCAL_REPAIR_PHASE, "timing_check_local_repair_failed",
            f"Local repair did not fix the drift-only tests: {failure[:500]}",
            rounds=rounds, tokens=tokens, seconds=round(time.monotonic() - started, 3),
            tests=flagged, reason=failure[-_TIMING_REPAIR_FEEDBACK_CHARS:],
        )
        return None


    @staticmethod
    def _timing_repair_task(
        flagged: list[str], tails: dict[str, str], drift_ms: str,
        changed: list[str], likely: tuple[str, ...],
    ) -> str:
        from modules.local_file_localizer import hint_line

        tail_text = "\n\n".join(
            f"### {test}\n{tails[test]}" for test in flagged if tails.get(test)
        ) or "(no failure text recorded)"
        return "\n".join([
            "Make these tests independent of wall-clock timing.",
            "They pass in a normal run but fail when the test clock runs fast "
            f"(the timing check adds {drift_ms or '?'} ms of drift to every clock read, "
            f"{timing_check_module.CLOCK_DRIFT_ENV}={drift_ms or '?'}).",
            "",
            "Flagged tests:",
            *(f"- {test}" for test in flagged),
            "",
            "Failure tails under drift:",
            tail_text,
            "",
            "Files of this change: " + (", ".join(changed) or "(none recorded)"),
            "",
            "Make the smallest change in the code or in the test that removes the "
            "dependence on wall-clock time, for example an injected clock, a larger "
            "deadline, or time.monotonic() instead of time.time(). Do not delete, skip "
            "or xfail a test and do not weaken what it checks. Never modify or delete any "
            "conftest.py, pytest.ini, pyproject.toml, setup.cfg, tox.ini, or any file "
            f"containing {timing_check_module.CLOCK_DRIFT_ENV} (the timing fixture and how "
            "pytest loads it): a repair that touches them is rejected.",
            hint_line(list(likely)),
        ]).rstrip()


    @staticmethod
    def _timing_repair_tokens(worker: Any, result: Any) -> int:
        metrics = getattr(worker, "last_run_metrics", None)
        if isinstance(metrics, dict):
            return int(metrics.get("tokens_in") or 0) + int(metrics.get("tokens_out") or 0)
        usage = getattr(result, "usage", None)
        if isinstance(usage, dict):
            return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        return 0


    def _judge_timing_repair(
        self, experiment_id: str, worktree_path: str, flagged: list[str],
        drift_env: dict[str, str], affected: list[str], paths: list[str],
        elog: ExperimentLogger, *, watchdog: WatchdogConfig | None = None,
        host_failures: list[str] | tuple[str, ...] = (),
        protected: set[str] | None = None,
        marker_files: set[str] | None = None,
    ) -> str:
        """"" when the candidate passes both runs, else why it does not.

        Tests that already failed on this host without drift (the control run)
        may fail in the run without drift again; any other failure rejects.
        The drift fixture and pytest config must still be intact when the drift
        rerun starts, otherwise that rerun could pass vacuously.
        """
        if protected is not None:
            missing = [
                path for path in sorted(protected)
                if not (Path(worktree_path) / path).is_file()
            ]
            broken = _drift_fixture_broken(worktree_path, marker_files or set())
            if missing or broken:
                gone = missing + [f"{path} (marker gone)" for path in broken if path not in missing]
                return f"the timing-drift fixture or pytest config changed: {', '.join(gone)}"

        def track(process: subprocess.Popen[str] | None) -> None:
            self._track_process(experiment_id, process)

        options: dict[str, Any] = (
            {"watchdog": watchdog, "run_id": f"{experiment_id}-timing-repair"} if watchdog else {}
        )
        tails: dict[str, str] = {}
        drift = timing_check_module.run_timing_check(
            worktree_path, flagged, on_process=track, python=self.test_python_executable,
            tails=tails, env=drift_env, **options,
        )
        if drift != "ok":
            return f"timing check under the same drift: {drift}\n" + "\n".join(tails.values())
        repair_diff = "".join(f"diff --git a/{path} b/{path}\n" for path in paths)
        files = list(dict.fromkeys([
            *affected,
            *(test.split("::", 1)[0] for test in flagged),
            *timing_check_module.affected_tests(repair_diff, worktree_path),
        ]))
        if watchdog:
            options["run_id"] = f"{experiment_id}-timing-repair-normal"
        tails = {}
        normal = timing_check_module.run_timing_check(
            worktree_path, files, 0, on_process=track, python=self.test_python_executable,
            tails=tails, **options,
        )
        still_failing = set(timing_check_module.flagged_tests(normal))
        if normal != "ok" and not (still_failing and still_failing <= set(host_failures)):
            return f"affected tests without drift: {normal}\n" + "\n".join(tails.values())
        return ""


    @staticmethod
    def _worktree_tree(worktree_path: str, elog: ExperimentLogger) -> str:
        """Tree object of the whole working state (tracked + untracked), index untouched."""
        with tempfile.TemporaryDirectory(prefix="nexus-timing-repair-index-") as index_dir:
            env = dict(os.environ)
            env["GIT_INDEX_FILE"] = str(Path(index_dir) / "index")
            for args in (["read-tree", "HEAD"], ["add", "-A"]):
                _run_git(args, cwd=worktree_path, env=env, elog=elog,
                         phase=TIMING_LOCAL_REPAIR_PHASE).check_returncode()
            proc = _run_git(["write-tree"], cwd=worktree_path, env=env, elog=elog,
                            phase=TIMING_LOCAL_REPAIR_PHASE)
            proc.check_returncode()
            return proc.stdout.strip()


    @staticmethod
    def _tree_diff_names(
        before: str, after: str, worktree_path: str, elog: ExperimentLogger,
    ) -> list[str]:
        if before == after:
            return []
        proc = _run_git(
            ["diff", "--name-only", "-z", "--no-renames", before, after],
            cwd=worktree_path, elog=elog, phase=TIMING_LOCAL_REPAIR_PHASE,
        )
        proc.check_returncode()
        return [path for path in proc.stdout.split("\0") if path]


    @staticmethod
    def _tree_diff(
        before: str, after: str, paths: list[str], worktree_path: str,
        elog: ExperimentLogger, *, stat: bool = False,
    ) -> str:
        proc = _run_git(
            ["--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
             "--binary", *(["--stat"] if stat else []), before, after, "--", *paths],
            cwd=worktree_path, elog=elog, phase=TIMING_LOCAL_REPAIR_PHASE,
        )
        proc.check_returncode()
        return proc.stdout


    def _restore_worktree_tree(
        self, worktree_path: str, tree: str, elog: ExperimentLogger,
    ) -> None:
        """Put every path the repair touched back to *tree*; the index is untouched."""
        current = self._worktree_tree(worktree_path, elog)
        paths = self._tree_diff_names(tree, current, worktree_path, elog)
        if not paths:
            return
        listing = _run_git(
            ["ls-tree", "-r", "-z", "--name-only", tree],
            cwd=worktree_path, elog=elog, phase=TIMING_LOCAL_REPAIR_PHASE,
        )
        listing.check_returncode()
        known = set(listing.stdout.split("\0"))
        existing = [path for path in paths if path in known]
        if existing:
            _run_git(
                ["--literal-pathspecs", "restore", f"--source={tree}", "--worktree",
                 "--", *existing],
                cwd=worktree_path, elog=elog, phase=TIMING_LOCAL_REPAIR_PHASE,
            ).check_returncode()
        root = Path(worktree_path)
        for path in paths:
            if path not in known:
                (root / path).unlink(missing_ok=True)
        if self._worktree_tree(worktree_path, elog) != tree:
            raise RuntimeError("the worktree differs from its state before the local repair")


    def _commit_timing_repair(
        self, worktree_path: str, before: str, paths: list[str], patch: str,
        elog: ExperimentLogger,
    ) -> TimingLocalRepair:
        """Commit only the repair's delta onto the branch HEAD; other work stays as it was.

        Every fallible step happens before the branch ref moves, and the one
        step after ``update-ref`` (refreshing the real index) rolls the ref back
        when it fails, so a crash can never leave HEAD on a repair commit whose
        worktree was undone. A patch that does not apply to HEAD - it overlaps
        uncommitted work - is rejected, never committed file-whole.
        """
        def git(args: list[str], env: dict[str, str] | None = None) -> str:
            proc = _run_git(args, cwd=worktree_path, env=env, elog=elog,
                            phase=TIMING_LOCAL_REPAIR_PHASE)
            proc.check_returncode()
            return proc.stdout

        head = git(["rev-parse", "HEAD"]).strip()
        with tempfile.TemporaryDirectory(prefix="nexus-timing-repair-commit-") as temp:
            env = dict(os.environ)
            env["GIT_INDEX_FILE"] = str(Path(temp) / "index")
            patch_file = Path(temp) / "repair.patch"
            patch_file.write_text(patch, encoding="utf-8")
            git(["read-tree", "HEAD"], env)
            # Not logged as a git command: a non-applying patch is an expected case.
            applied = _run_git(
                ["apply", "--cached", "--binary", str(patch_file)], cwd=worktree_path, env=env,
            )
            if applied.returncode != 0:
                raise _TimingRepairRejected(
                    "the candidate patch does not apply to the branch HEAD (it overlaps "
                    f"uncommitted work in: {', '.join(paths)}); the candidate is discarded"
                )
            tree = git(["write-tree"], env).strip()
        # commit-tree moves no ref: a failure below leaves branch and worktree as they were.
        commit = git([
            "commit-tree", tree, "-p", head, "-m", TIMING_LOCAL_REPAIR_COMMIT_MESSAGE,
        ]).strip()
        # Measured tree-to-tree, before any ref moves.
        stat = self._tree_diff(before, tree, paths, worktree_path, elog, stat=True)
        git(["update-ref", "-m", TIMING_LOCAL_REPAIR_COMMIT_MESSAGE, "HEAD", commit, head])
        try:
            # The real index still describes the old HEAD for these paths.
            _run_git(
                ["--literal-pathspecs", "reset", "-q", commit, "--", *paths],
                cwd=worktree_path, elog=elog, phase=TIMING_LOCAL_REPAIR_PHASE,
            ).check_returncode()
        except Exception:
            # Roll the branch back before the caller restores the worktree, so
            # HEAD can never stay on a repair commit whose files were undone.
            try:
                _run_git(
                    ["update-ref", "-m", f"{TIMING_LOCAL_REPAIR_COMMIT_MESSAGE} (rollback)",
                     "HEAD", head, commit],
                    cwd=worktree_path, elog=elog, phase=TIMING_LOCAL_REPAIR_PHASE,
                ).check_returncode()
            except Exception as rollback_exc:  # noqa: BLE001 - reported, then re-raise
                elog.error(
                    TIMING_LOCAL_REPAIR_PHASE, "timing_check_local_repair_rollback_failed",
                    "Could not roll the branch back after a failed repair commit",
                    exc=rollback_exc, commit_sha=commit, head=head,
                )
            raise
        return TimingLocalRepair(commit_sha=commit, paths=paths, diff=patch, stat=stat)


    def _timing_base_drift(
        self, experiment_id: str, worktree_path: str, flagged: list[str],
        drift_env: dict[str, str] | None, drift_tails: dict[str, str],
        elog: ExperimentLogger, *, watchdog: WatchdogConfig | None = None,
        github_work_tracking: Any | None = None,
    ) -> tuple[list[str], list[str]] | None:
        """``(regressed, base_drift)`` against the base commit (#725), else None.

        None (no base commit, no verdict, a crash) keeps the flag a failure -
        the old fail-closed behaviour. Base-drift tests are stored in
        ``known_drift_tests`` whatever the verdict for the others.
        """
        base_commit = self.get_experiment(experiment_id).base_commit_sha
        if not base_commit or drift_env is None:
            elog.info(
                "verifying", "timing_base_check_skipped",
                "Experiment has no recorded base commit", reason="no_base_commit",
            )
            return None
        options: dict[str, Any] = (
            {"watchdog": watchdog, "run_id": f"{experiment_id}-timing-base"} if watchdog else {}
        )
        try:
            base = timing_check_module.base_drift_check(
                base_commit, worktree_path, flagged, drift_env,
                python=self.test_python_executable,
                on_process=lambda process: self._track_process(experiment_id, process),
                **options,
            )
        except HungTest:
            raise
        except Exception as exc:  # noqa: BLE001 - no base verdict keeps the failure
            elog.error(
                "verifying", "timing_base_check_failed",
                "Could not rerun the flagged tests on the base commit", exc=exc,
            )
            return None
        if base is None:
            elog.warning(
                "verifying", "timing_base_check_failed",
                "The drift rerun on the base commit gave no verdict", base_commit=base_commit,
            )
            return None
        regressed, base_drift = base
        elog.info(
            "verifying", "timing_base_check_classified",
            "Classified flagged tests against the base commit under the same drift",
            base_commit=base_commit, regressed=regressed, base_drift=base_drift,
        )
        if base_drift:
            try:
                self._remember_known_drift(
                    experiment_id, base_commit, base_drift, drift_tails, elog,
                    github_work_tracking=github_work_tracking,
                )
            except Exception as exc:  # noqa: BLE001 - bookkeeping never changes the verdict
                elog.error(
                    "verifying", "known_drift_record_failed",
                    "Could not store the known drift tests", exc=exc,
                )
        return regressed, base_drift


    def _remember_known_drift(
        self, experiment_id: str, base_commit: str, test_ids: list[str],
        drift_tails: dict[str, str], elog: ExperimentLogger, *,
        github_work_tracking: Any | None = None,
    ) -> None:
        """Count each sighting; a test without an issue yet gets exactly one."""
        from modules import known_drift_tests
        from modules.redaction import redact

        now = _utcnow_iso()
        for test_id in test_ids:
            with self._lock:
                new = known_drift_tests.record_sighting(
                    self._conn, test_id, base_commit, now, experiment_id,
                )
                wants_issue = known_drift_tests.needs_issue(self._conn, test_id)
            open_issue = getattr(github_work_tracking, "open_issue", None)
            if not wants_issue or open_issue is None:
                continue
            try:
                opened = open_issue(
                    known_drift_tests.issue_title(test_id),
                    redact(known_drift_tests.issue_body(
                        test_id, base_commit, drift_tails.get(test_id),
                        experiment_id=experiment_id,
                    )),
                )
            except Exception as exc:  # noqa: BLE001 - retried at the next sighting
                elog.warning(
                    "verifying", "known_drift_issue_failed",
                    f"Could not open the issue for {test_id}: {type(exc).__name__}",
                )
                continue
            number = (opened or {}).get("number")
            if number:
                with self._lock:
                    known_drift_tests.set_issue(self._conn, test_id, int(number))
                elog.info(
                    "verifying", "known_drift_issue_opened",
                    f"Known drift test {test_id} -> issue #{number}",
                    test_id=test_id, issue_number=int(number), first_sighting=new,
                )


    def _next_verification_iteration(self, experiment_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(iteration), -1) + 1 FROM verification_runs "
                "WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
        return int(row[0])

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


    def _verification_diff(
        self, worktree_path: str, elog: ExperimentLogger,
    ) -> tuple[str, str, str]:
        """Read the stat, patch, and check report from one worktree snapshot."""
        with tempfile.TemporaryDirectory(prefix="experiment-git-index-") as index_dir:
            index_env = dict(os.environ)
            index_env["GIT_INDEX_FILE"] = str(Path(index_dir) / "index")

            def git(args: list[str]) -> subprocess.CompletedProcess:
                return _run_git(
                    args, cwd=worktree_path, env=index_env, elog=elog, phase="verifying",
                )

            git(["read-tree", "HEAD"]).check_returncode()
            git(["add", "-A"]).check_returncode()
            stat = git(["diff", "--cached", "--stat", "HEAD"])
            stat.check_returncode()
            diff = git(["diff", "--cached", "HEAD"])
            diff.check_returncode()
            check = git(["diff", "--cached", "--check", "HEAD"])
            report = diff_check_report(check)
            return stat.stdout, diff.stdout, report

    def _verification_numstat(
        self, experiment_id: str, worktree_path: str, elog: ExperimentLogger,
    ) -> DiffSize | None:
        """``git diff --numstat <base>`` of the staged worktree (#821); free.

        Staged in a throwaway index like :meth:`_verification_diff`, so new
        files count too. ``None`` (stored as NULL) when it cannot be measured;
        the size never changes a verdict.
        """
        base = self.get_experiment(experiment_id).base_commit_sha or "HEAD"
        try:
            with tempfile.TemporaryDirectory(prefix="experiment-git-index-") as index_dir:
                index_env = dict(os.environ)
                index_env["GIT_INDEX_FILE"] = str(Path(index_dir) / "index")

                def git(args: list[str]) -> subprocess.CompletedProcess:
                    return _run_git(
                        args, cwd=worktree_path, env=index_env, elog=elog, phase="verifying",
                    )

                git(["read-tree", "HEAD"]).check_returncode()
                git(["add", "-A"]).check_returncode()
                proc = git(["diff", "--cached", "--numstat", "--no-color", base])
                proc.check_returncode()
                size = parse_numstat(str(proc.stdout or ""))
        except Exception as exc:  # noqa: BLE001 - a missing size is NULL, never a verdict
            elog.warning(
                "verifying", "diff_numstat_failed",
                "Could not measure the diff size with git diff --numstat", exc=exc,
            )
            return None
        elog.info(
            "verifying", "diff_numstat_recorded",
            f"Diff against {base[:12]}: {size.files} file(s), +{size.added} -{size.removed}",
            diff_files=size.files, diff_added=size.added, diff_removed=size.removed,
        )
        return size

    def _store_diff_size(self, experiment_id: str, size: DiffSize | None) -> None:
        """Persist the measured diff size of the running verification (#821).

        All three values are written together, immediately after staging, so
        a monitored test that raises or an external stop afterwards cannot
        lose the measurement. The UPDATE is guarded by the still-active
        verification state, so a stale pipeline thread can never overwrite
        the evidence of a newer attempt. ``None`` stores NULL (unmeasured).
        """
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE experiments SET diff_files = ?, diff_added = ?, diff_removed = ? "
                    "WHERE id = ? AND status = ?",
                    (
                        size.files if size is not None else None,
                        size.added if size is not None else None,
                        size.removed if size is not None else None,
                        experiment_id,
                        STATUS_VERIFYING,
                    ),
                )
        except sqlite3.Error as exc:  # noqa: BLE001 - a missing size is NULL, never a verdict
            logger.warning("experiment %s: could not store the diff size: %s", experiment_id, exc)

    def _clear_diff_size(self, experiment_id: str) -> None:
        """Drop a stale size when a new verification starts (#821).

        Same guard as :meth:`_store_diff_size`: a repair iteration or a fresh
        run never keeps the previous attempt's numbers.
        """
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE experiments SET diff_files = NULL, diff_added = NULL, "
                    "diff_removed = NULL WHERE id = ? AND status = ?",
                    (experiment_id, STATUS_VERIFYING),
                )
        except sqlite3.Error as exc:  # noqa: BLE001 - the next measurement overwrites anyway
            logger.warning("experiment %s: could not clear the diff size: %s", experiment_id, exc)

    def _autofix_diff_check_whitespace(
        self,
        worktree_path: str,
        diff_check_issue: str,
        elog: ExperimentLogger,
    ) -> tuple[str, dict[str, int], tuple[str, str] | None]:
        """Apply one fix and return the report, changed lines, and refreshed diff."""
        fixed_files: dict[str, int] = {}
        try:
            fix_result = fix_whitespace(worktree_path, "HEAD")
            fixed_files = fix_result.fixed_files
            if not fixed_files:
                return fix_result.remaining_report or diff_check_issue, fixed_files, None
            diff_stat, diff_text, remaining = self._verification_diff(worktree_path, elog)
            _run_git(
                ["--literal-pathspecs", "add", "--", *sorted(fixed_files)],
                cwd=worktree_path, elog=elog, phase="verifying",
            ).check_returncode()
        except Exception as exc:  # noqa: BLE001 - an autofix failure cannot clear the original finding
            elog.warning(
                "verifying", "diff_check_autofix_failed",
                "Whitespace autofix could not complete; keeping the original git diff --check report",
                exc=exc,
            )
            return diff_check_issue, fixed_files, None
        elog.info(
            "verifying", "diff_check_autofixed",
            f"Auto-fixed {fix_result.fixed_line_count} whitespace line(s) reported by "
            f"git diff --check in {len(fixed_files)} file(s)",
            files=sorted(fixed_files),
            lines_fixed=fix_result.fixed_line_count,
            files_line_counts=fixed_files,
            remaining_report=remaining[:500] or None,
        )
        return remaining, fixed_files, (diff_stat, diff_text)

    def _verify(
        self,
        experiment_id: str,
        worktree_path: str,
        elog: ExperimentLogger | None = None,
        *,
        github_work_tracking: Any | None = None,
        repair_iteration: int = 0,
        soft_findings_to_reviewer: bool | None = None,
        abort_on_test_stall: bool = False,
    ) -> Experiment:
        if elog is None:
            elog = self.get_experiment_logger(experiment_id)
        # #821: a new verification starts from no size; the measurement below
        # stores its own values immediately after staging.
        self._clear_diff_size(experiment_id)
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
            diff_stat, full_diff, diff_check_issue = self._verification_diff(worktree_path, elog)
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
        if git_status.strip():
            elog.debug("verifying", "git_status_checked", "Worktree has uncommitted changes as expected")
        else:
            elog.warning("verifying", "git_status_clean", "Worktree has no changes after implementation phase")

        # #817: a whitespace-only `git diff --check` finding is mechanical dirt, not a
        # verdict - fix it in the worktree once and let only the remaining report count.
        diff_check_autofixed: dict[str, int] = {}
        if diff_check_issue:
            diff_check_issue, diff_check_autofixed, refreshed_diff = (
                self._autofix_diff_check_whitespace(worktree_path, diff_check_issue, elog)
            )
            if refreshed_diff is not None:
                diff_stat, full_diff = refreshed_diff
        diff_text = _truncate_text(full_diff, self.max_diff_bytes, label="diff")
        # #821: the size of the final staged diff, next to the later review verdict.
        diff_size = self._verification_numstat(experiment_id, worktree_path, elog)
        # Persisted right away, all three values together: a monitored test or
        # timing check that raises, or an external stop, must not lose it.
        self._store_diff_size(experiment_id, diff_size)
        # NULL when unmeasured, so a repair iteration never keeps an older size.
        diff_size_fields = {
            "diff_files": diff_size.files if diff_size is not None else None,
            "diff_added": diff_size.added if diff_size is not None else None,
            "diff_removed": diff_size.removed if diff_size is not None else None,
        }

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
                **diff_size_fields,
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
                tests_only_progress=abort_on_test_stall,
                # Publishing the test heartbeat into this experiment's log keeps the global Nexus
                # heartbeat alive during a long suite; per-test entries stay in the dedicated test log.
                on_heartbeat=global_heartbeat_bridge(elog, "verifying"),
                on_process=lambda process: self._track_process(experiment_id, process),
            )
            if abort_on_test_stall and test_run.stalled:
                test_id = test_run.aborted_test_id
                reason = test_id or "no active unittest reported"
                self._record_verification_run(
                    experiment_id, self._next_verification_iteration(experiment_id),
                    "hung_test", test_command, None,
                    _truncate_text(test_run.output, self.max_test_output_bytes, label="test output"),
                    reason,
                )
                raise HungTest(reason)
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

        # #817: mention the whitespace auto-fix in the verification summary so the
        # reviewer sees it next to the test output (stored record + PR diagnostics).
        diff_check_autofix_note: str | None = None
        if diff_check_autofixed:
            diff_check_autofix_note = (
                f"git diff --check whitespace auto-fixed in {len(diff_check_autofixed)} "
                f"file(s), {sum(diff_check_autofixed.values())} line(s): "
                + ", ".join(sorted(diff_check_autofixed))
            )
            note = f"[diff_check_autofixed] {diff_check_autofix_note}"
            # Reserve space for the note while keeping the CI evidence line first.
            output_budget = self.max_test_output_bytes - len(note.encode("utf-8", errors="replace")) - 1
            summary_output = _truncate_text(test_output, max(0, output_budget), label="test output")
            test_output_capped = f"{summary_output}\n{note}" if summary_output else note

        final_status = STATUS_SUCCEEDED
        error: str | None = None
        quality_evaluation: dict[str, Any] | None = None
        all_failures_pre_existing = bool(baseline_classifications) and all(
            value == "pre_existing" for value in baseline_classifications.values()
        )
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
            if diff_check_autofix_note:
                error = f"{error}; {diff_check_autofix_note}"
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
        else:
            elog.info("verifying", "tests_passed", "Verification tests passed")
            try:
                options = {"watchdog": self._get_test_watchdog_config()} if abort_on_test_stall else {}
                getattr(self, "_timing_local_repairs", {}).pop(experiment_id, None)
                timing_failure = self._run_timing_check(
                    experiment_id, worktree_path, full_diff, elog,
                    github_work_tracking=github_work_tracking, **options,
                )
            except HungTest as exc:
                self._record_verification_run(
                    experiment_id, self._next_verification_iteration(experiment_id),
                    "hung_test", test_command, None, test_output_capped, str(exc),
                )
                raise
            if self._ended_from_outside(experiment_id):
                return self.get_experiment(experiment_id)
            if timing_failure is not None:
                final_status, error = timing_failure
            repair = getattr(self, "_timing_local_repairs", {}).pop(experiment_id, None)
            if repair is not None:
                # #711: the committed repair is part of what was verified.
                listed = {line[3:] for line in git_status.splitlines() if len(line) > 3}
                git_status += "".join(f" M {path}\n" for path in repair.paths if path not in listed)
                diff_stat = f"{diff_stat}\n[{TIMING_LOCAL_REPAIR_COMMIT_MESSAGE}]\n{repair.stat}"
                full_diff = f"{full_diff}{repair.diff}"
                diff_text = _truncate_text(full_diff, self.max_diff_bytes, label="diff")
                diff_size = self._verification_numstat(experiment_id, worktree_path, elog)
                self._store_diff_size(experiment_id, diff_size)
                diff_size_fields = {
                    "diff_files": diff_size.files if diff_size is not None else None,
                    "diff_added": diff_size.added if diff_size is not None else None,
                    "diff_removed": diff_size.removed if diff_size is not None else None,
                }

        # Every branch above except a fully green run (tests and timing check)
        # has already moved final_status away from succeeded.
        if final_status == STATUS_SUCCEEDED:
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
                # adoption (#519); every non-GitHub-tracked run keeps the hard stop. A
                # re-verification of a GitHub-tracked run (#718) asks for the same routing.
                to_reviewer = (
                    github_work_tracking is not None
                    if soft_findings_to_reviewer is None else soft_findings_to_reviewer
                )
                review_findings = eval_result.soft_findings() if to_reviewer else None
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

        self._record_verification_run(
            experiment_id,
            self._next_verification_iteration(experiment_id),
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
            **diff_size_fields,
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

