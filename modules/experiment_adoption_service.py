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
    _TEST_COMMANDS,
    _DEFAULT_TEST_COMMAND_KEY,
    _run_git,
    _truncate_text,
    _utcnow_iso,
)
from modules.github_work_tracking import (
    LIFECYCLE_READINESS_MESSAGE,
    GitHubCIVerification,
    GitHubWorkTrackingError,
    MergeForbiddenError,
    GitHubWorkTrackingService,
)
from modules.mailbox import MailboxLock
from modules.redaction import bounded_lines, redact, sanitized_reason
from modules.run_store import DEFAULT_DB_PATH, init_db
from modules.test_run_log import global_heartbeat_bridge
from modules.verification_runner import run_verification_test_monitored


logger = logging.getLogger(__name__)


from modules.experiment_adoption_parts.core import (
    ADOPTION_THREAD_PREFIX,
    AuthoritativeCI,
    ExperimentAdoption,
    ExperimentAdoptionError,
    REVIEW_BLOCKER_GITHUB_LIFECYCLE,
    STATUS_ADOPTED,
    STATUS_ADOPTING,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_NEEDS_REVIEW,
    STATUS_NOT_STARTED,
    STATUS_QUEUED,
    STATUS_READY_FOR_REVIEW,
    StaleFailureAssessment,
    _ACTIVE_STATUSES,
    _DEFAULT_GIT_TIMEOUT_SECONDS,
    _DEFAULT_LOCK_PATH,
    _DEFAULT_TARGET_BRANCH,
    _MAX_RECOVERY_ATTEMPTS,
    _MAX_STALE_RETRIES,
    _NullExperimentLogger,
    _TERMINAL_STATUSES,
    _collapse_summary,
    _json_or_none,
    _normalize_path,
    _row_to_adoption,
    _safe_list,
)
from modules.experiment_adoption_parts.lifecycle import LifecycleMixin
from modules.experiment_adoption_parts.integration import IntegrationMixin
from modules.experiment_adoption_parts.verification import VerificationMixin
from modules.experiment_adoption_parts.github_pr import GitHubPrMixin
from modules.experiment_adoption_parts.failure import FailureMixin
from modules.experiment_adoption_parts.pipeline import PipelineMixin


class ExperimentAdoptionService(
    LifecycleMixin,
    IntegrationMixin,
    VerificationMixin,
    GitHubPrMixin,
    FailureMixin,
    PipelineMixin,
):
    """Persisted, exactly-once experiment adoption state machine.

    Adoption publishes only through the mandatory GitHub lifecycle: append to
    the Issue-linked branch, open/reuse its PR, mark the verified Draft ready
    and merge it. Direct pushes to main are not allowed.
    """

