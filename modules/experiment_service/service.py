from __future__ import annotations

import json
import logging
import os
import re
import signal
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from modules.coding_provider import (
    build_random_worker,
    build_worker,
    is_provider_overload,
    other_provider,
    provider_of,
    random_worker_factory,
)
from modules.codex_worker import CodexResult, CodexWorker
from modules.coding_worker import agent_watchdog_limits
from modules.experiment_evaluation import parse_git_status_short
from modules.central_logging import (
    DEFAULT_LOG_DIR,
    ExperimentLogger,
    _tail_read_lines,
    sanitize_command,
)
from modules.github_work_tracking import failing_test_names
from modules.redaction import sanitized_reason
from modules.run_store import (
    DEFAULT_DB_PATH,
    get_setting,
    init_db,
    insert_activity,
    list_activity,
    set_setting,
)
from modules.test_run_log import global_heartbeat_bridge
from modules.verification_runner import (
    VerificationRunResult,
    WatchdogConfig,
    run_verification_test_monitored,
)

from .git_utils import _run_git
from .models import (
    Experiment,
    ExperimentAlreadyActiveError,
    ExperimentNotFoundError,
    ExperimentNotRunnableError,
    ExperimentNotCancellableError,
    PIPELINE_CRASH_ERROR_PREFIX,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_IMPLEMENTING,
    STATUS_INTERRUPTED,
    STATUS_NEEDS_REVIEW,
    STATUS_QUEUED,
    STATUS_REJECTED,
    STATUS_SELECTING,
    STATUS_SUCCEEDED,
    STATUS_TIMED_OUT,
    STATUS_VERIFYING,
    _TERMINAL_STATUSES,
    _row_to_experiment,
)
from .policy import _check_goal_policy, _goal_secret_warning, _policy_rule_for_reason
from .prompts import _build_direct_implementation_prompt, _build_repair_implementation_prompt
from .text_utils import _truncate_text, _utcnow_iso

logger = logging.getLogger("modules.experiment_service")


from .service_parts.core import (
    BASELINE_RECHECK_ENV_VAR,
    DEFAULT_BASELINE_RECHECK_SECONDS,
    DEFAULT_STALL_GRACE_SECONDS,
    DEFAULT_WORKTREE_RETENTION_HOURS,
    EXPERIMENT_TOKEN_BUDGET_ENV_VAR,
    STALL_GRACE_ENV_VAR,
    WORKTREE_RETENTION_ENV_VAR,
    WorktreeCleanupResult,
    _ADOPTION_TERMINAL_STATUSES,
    _DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS,
    _DEFAULT_MAX_DIAGNOSTIC_LOG_BYTES,
    _DEFAULT_MAX_DIFF_BYTES,
    _DEFAULT_MAX_RAW_MESSAGE_BYTES,
    _DEFAULT_MAX_REPAIR_ITERATIONS,
    _DEFAULT_MAX_TEST_OUTPUT_BYTES,
    _DEFAULT_PLAN_TIMEOUT_SECONDS,
    _DEFAULT_RESEARCH_TIMEOUT_SECONDS,
    _DEFAULT_TEST_COMMAND_KEY,
    _DEFAULT_TEST_PYTHON_EXECUTABLE,
    _ERROR_STATUSES,
    _GITHUB_ISSUE_GOAL_RE,
    _INTERRUPTED_BY_RESTART_ERROR,
    _MAX_ERROR_TEST_NAMES,
    _REFORMAT_RETRY_TIMEOUT_SECONDS,
    _STALLED_ERROR_PREFIX,
    _STALL_LOG_TAIL_BYTES,
    _STALL_SWEEP_STATUSES,
    _STALL_TERMINATE_GRACE_SECONDS,
    _TEST_COMMANDS,
    _TEST_TOTAL_SETTING_KEY,
    _WARNING_STATUSES,
    _baseline_verdict,
    _experiment_token_budget,
    _is_real_test_id,
    _is_stalled,
    _parse_utc,
    _test_file_for_id,
    stall_grace_seconds,
    worker_run_token_total,
    worktree_retention_hours,
)
from .service_parts.records import RecordsMixin
from .service_parts.diagnostics import DiagnosticsMixin
from .service_parts.implementation import ImplementationMixin, _final_issue_progress
from .service_parts.verification import VerificationMixin
from .service_parts.stall import StallSweepMixin
from .service_parts.cleanup import CleanupMixin


class ExperimentService(
    RecordsMixin,
    DiagnosticsMixin,
    ImplementationMixin,
    VerificationMixin,
    StallSweepMixin,
    CleanupMixin,
):
    """Orchestrates one bounded self-development experiment at a time, using the
    external coding-agent workers (``CodexWorker``/``ClaudeWorker``) for the read-only
    selection phase and the isolated implementation phase. The provider is chosen by
    a persistent rotation (``modules.coding_provider``) unless a worker/factory is
    injected, and recorded on the experiment as ``implementation_provider``.
    """
