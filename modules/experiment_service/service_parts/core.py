"""Constants and module-level helpers of the experiment service."""

from __future__ import annotations

import logging
import os
import re

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from modules.verification_runner import VerificationRunResult

from ..models import (
    STATUS_FAILED,
    STATUS_IMPLEMENTING,
    STATUS_NEEDS_REVIEW,
    STATUS_QUEUED,
    STATUS_SELECTING,
    STATUS_VERIFYING,
)


logger = logging.getLogger("modules.experiment_service")

_GITHUB_ISSUE_GOAL_RE = re.compile(r"^GitHub Issue #(\d+):")

# Statuses worth journalling at WARNING (vs INFO); kept as a small fixed set here
# so lifecycle logging in ``_transition``/``_force_status`` stays centralized.
_WARNING_STATUSES = frozenset(
    {"rejected", "failed", "timed_out", "needs_review", "cancelled", "interrupted"}
)

# Subset of the above that is an actual failure (vs a policy rejection or review
# handoff), logged at ERROR in the per-experiment JSONL log.
_ERROR_STATUSES = frozenset({"failed", "timed_out"})

_DEFAULT_MAX_TEST_OUTPUT_BYTES = 32 * 1024
_MAX_ERROR_TEST_NAMES = 3
_DEFAULT_MAX_DIFF_BYTES = 64 * 1024
_DEFAULT_MAX_RAW_MESSAGE_BYTES = 16 * 1024
_DEFAULT_MAX_DIAGNOSTIC_LOG_BYTES = 64 * 1024
_DEFAULT_RESEARCH_TIMEOUT_SECONDS = 120
_DEFAULT_PLAN_TIMEOUT_SECONDS = 90
_REFORMAT_RETRY_TIMEOUT_SECONDS = 60
# Absolute cap only (#327): the coding agent is progress-watched and stopped when
# it stalls; see modules.coding_worker.agent_watchdog_limits.
_DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS = 3 * 60 * 60
_DEFAULT_MAX_REPAIR_ITERATIONS = 2
BASELINE_RECHECK_ENV_VAR = "NEXUS_BASELINE_RECHECK_SECONDS"
DEFAULT_BASELINE_RECHECK_SECONDS = 120.0
EXPERIMENT_TOKEN_BUDGET_ENV_VAR = "NEXUS_EXPERIMENT_TOKEN_BUDGET"
EXPERIMENT_TOKEN_BUDGET_MODE_ENV_VAR = "NEXUS_EXPERIMENT_TOKEN_BUDGET_MODE"

# Error recorded on experiments whose thread died with the process (#281).
_INTERRUPTED_BY_RESTART_ERROR = "interrupted by service restart"

# Worktree retention (#376): worktrees are kept after an experiment ends for
# inspection/retry/adoption, but a full checkout per run exhausts a 1 GB VM's disk,
# so terminal runs older than this many hours lose the directory (row, log and branch
# are kept). ``0`` or anything unparsable disables cleanup - a typo must not start deleting.
WORKTREE_RETENTION_ENV_VAR = "NEXUS_EXPERIMENT_WORKTREE_RETENTION_HOURS"
DEFAULT_WORKTREE_RETENTION_HOURS = 7 * 24

# Adoption statuses that still need the experiment's worktree, whatever its age.
_ADOPTION_TERMINAL_STATUSES = ("adopted", "needs_review", "failed", "interrupted")

# Stall sweep (#400): fail an active experiment whose newest log or activity event
# is older than its phase's hard limit plus this grace. A live worker logs
# ``coding_agent_progress`` every 10 minutes, so a healthy run never nears the limit.
STALL_GRACE_ENV_VAR = "NEXUS_EXPERIMENT_STALL_GRACE_SECONDS"
DEFAULT_STALL_GRACE_SECONDS = 15 * 60
_STALLED_ERROR_PREFIX = "stalled: "
_STALL_SWEEP_STATUSES = (STATUS_QUEUED, STATUS_SELECTING, STATUS_IMPLEMENTING, STATUS_VERIFYING)
# Time a stalled worker's process group gets after SIGTERM before SIGKILL.
_STALL_TERMINATE_GRACE_SECONDS = 5.0
# Only the end of an experiment log is read to find its newest entry.
_STALL_LOG_TAIL_BYTES = 64 * 1024


def stall_grace_seconds(raw: str | None = None) -> float:
    """Configured stall grace in seconds; a typo falls back to the default."""
    value = raw if raw is not None else os.environ.get(STALL_GRACE_ENV_VAR)
    if value is None or not str(value).strip():
        return float(DEFAULT_STALL_GRACE_SECONDS)
    try:
        seconds = float(str(value).strip())
    except ValueError:
        logger.warning("ignoring invalid %s value; using the default", STALL_GRACE_ENV_VAR)
        return float(DEFAULT_STALL_GRACE_SECONDS)
    if seconds < 0 or seconds != seconds or seconds == float("inf"):
        return float(DEFAULT_STALL_GRACE_SECONDS)
    return seconds


def _is_stalled(status: str | None, error: str | None) -> bool:
    """Whether the stall sweep ended this experiment (#400)."""
    return status == STATUS_FAILED and str(error or "").startswith(_STALLED_ERROR_PREFIX)


# Error prefix of experiments ended by the token budget in enforce mode (#533).
TOKEN_BUDGET_STOP_PREFIX = "experiment token budget exceeded"


def _is_budget_stop(status: str | None, error: str | None) -> bool:
    """Whether enforce mode ended this experiment for its token budget (#533)."""
    return status == STATUS_NEEDS_REVIEW and str(error or "").startswith(TOKEN_BUDGET_STOP_PREFIX)


def worktree_retention_hours(raw: str | None = None) -> float:
    """Configured worktree retention in hours; ``0`` disables cleanup."""
    value = raw if raw is not None else os.environ.get(WORKTREE_RETENTION_ENV_VAR)
    if value is None or not str(value).strip():
        return float(DEFAULT_WORKTREE_RETENTION_HOURS)
    try:
        hours = float(str(value).strip())
    except ValueError:
        logger.warning("ignoring invalid %s value; worktree cleanup disabled", WORKTREE_RETENTION_ENV_VAR)
        return 0.0
    return hours if hours > 0 and hours != float("inf") else 0.0


def _experiment_token_budget(raw: str | None) -> int | None:
    """Parse the optional shadow-mode budget; invalid values disable it safely."""
    if raw is None or not raw.strip():
        return None
    try:
        budget = int(raw)
    except ValueError:
        logger.warning("ignoring invalid %s value", EXPERIMENT_TOKEN_BUDGET_ENV_VAR)
        return None
    if budget < 0:
        logger.warning("ignoring negative %s value", EXPERIMENT_TOKEN_BUDGET_ENV_VAR)
        return None
    return budget


def worker_run_token_total(usage: dict[str, Any] | None) -> int:
    """Return provider-independent tokens for one completed worker run.

    Both workers flatten their final usage event.  Input and output tokens are
    counted along with every reported cache-token counter; unrelated counters
    (for example reasoning tokens) are deliberately excluded.
    """
    if not isinstance(usage, dict):
        return 0
    keys = {"input_tokens", "output_tokens"}
    keys.update(
        key for key in usage
        if isinstance(key, str) and "cache" in key.lower() and "token" in key.lower()
    )
    return sum(
        max(0, int(usage.get(key, 0)))
        for key in keys
        if isinstance(usage.get(key, 0), (int, float))
        and not isinstance(usage.get(key, 0), bool)
    )


def _parse_utc(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(timestamp))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class WorktreeCleanupResult:
    """What one worktree retention pass did, for logging and tests."""

    retention_hours: float
    removed: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

# Progress-aware local verification (#209): the run is watched for *progress* (tests
# starting/finishing), not bounded by a wall-clock limit, which cannot tell a slow
# suite from a stuck test; the absolute cap below is only a final guard against a
# permanently orphaned process. The last reported test total is persisted in
# ``service_settings`` so a run can report "842 / 1873"; watchdog numbers stay
# tunable through the environment without editing code.
_TEST_TOTAL_SETTING_KEY = "verification_test_total"

# Built-in default test commands, keyed by a fixed identifier: plan/LLM output never
# supplies a raw shell string. "{python}" becomes the configured interpreter so
# worktrees without their own venv can run the suite. ``-v`` because the verbose
# stream is the in-band progress signal the watchdog is built on (the runner strips
# the per-test "... ok" lines back out again).
_TEST_COMMANDS: dict[str, list[str]] = {
    "unittest-discover": ["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"],
}
_DEFAULT_TEST_COMMAND_KEY = "unittest-discover"


def _is_real_test_id(test_id: str) -> bool:
    """Whether *test_id* names a collected test rather than a loader error."""
    return "_FailedTest." not in test_id


def _test_file_for_id(test_id: str) -> str | None:
    """Source file of a canonical ID from ``unittest discover -s tests``.

    A loader error is named ``unittest.loader._FailedTest.<module>``; the file
    it points at is that of *<module>*, which could not be imported.
    """
    _, marker, loader_target = test_id.rpartition("_FailedTest.")
    parts = (loader_target if marker else test_id).split(".")
    if parts[0] == "tests":
        parts = parts[1:]
    for index, part in enumerate(parts):
        if part.startswith("test"):
            return "/".join(["tests", *parts[: index + 1]]) + ".py"
    return None


def _baseline_verdict(test_id: str, result: VerificationRunResult) -> str:
    """Classify one test from its run at the base commit.

    Only a result for this very test exculpates the change. Selecting by
    ``-k`` matches substrings, so the run's exit code alone may belong to a
    sibling; and a run which never started the test (a deadline hit while
    importing, a selector that matched nothing) says nothing about it.
    """
    run_failed = result.timed_out or result.exit_code not in (0, None)
    if (
        run_failed
        and test_id in (result.observed_test_ids or [])
        and test_id in (result.failed_test_ids or [])
    ):
        return "pre_existing"
    return "regression"


# Default test interpreter inside a worktree: the main repo's venv python, so no files
# are copied and no package installed - the worktree is only the command's cwd.
_DEFAULT_TEST_PYTHON_EXECUTABLE = "/agent/.venv/bin/python"

