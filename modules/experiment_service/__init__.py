"""Controlled, single-run self-development experiment service.

This package preserves the historical ``modules.experiment_service``
import surface while splitting the implementation into focused
submodules grouped by responsibility. ``ExperimentService`` still
orchestrates exactly one bounded self-improvement attempt end to end.
"""

import __future__
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from modules.codex_worker import CodexResult, CodexWorker
from modules.experiment_evaluation import (
    ChangedFiles,
    evaluate_implementation,
    parse_git_status_short,
    validate_plan as validate_plan_completeness,
)
from modules.central_logging import DEFAULT_LOG_DIR, ExperimentLogger, sanitize_command
from modules.redaction import sanitized_reason
from modules.run_store import (
    DEFAULT_DB_PATH,
    get_setting,
    init_db,
    insert_activity,
    list_activity,
    set_setting,
)
from modules.verification_runner import run_verification_test

annotations = __future__.annotations

from .git_utils import _run_git
from .models import (
    EXPERIMENT_STATUSES,
    STATUS_FAILED,
    STATUS_IMPLEMENTING,
    STATUS_NEEDS_REVIEW,
    STATUS_QUEUED,
    STATUS_REJECTED,
    STATUS_SELECTING,
    STATUS_SUCCEEDED,
    STATUS_TIMED_OUT,
    STATUS_VERIFYING,
    Experiment,
    ExperimentNotFoundError,
    ExperimentNotRunnableError,
    ExperimentPlan,
    _STATE_TRANSITIONS,
    _TERMINAL_STATUSES,
    _row_to_experiment,
)
from .parsing import (
    PLAN_JSON_SCHEMA,
    _PLAN_FIELD_PATTERNS,
    _find_balanced_json_objects,
    extract_single_json_object,
    parse_plan,
)
from .policy import (
    _CREDENTIAL_QUALIFIED_TOKEN_REGEX,
    _DOCUMENTATION_ONLY_SUFFIXES,
    _GIT_ACTION_KEYWORDS,
    _GIT_ACTION_MAX_FILLER,
    _GIT_ACTION_REGEXES,
    _GIT_NOUN_ALT,
    _GIT_OBJECT_NOUNS,
    _INFRA_ACTION_MAX_FILLER_WORDS,
    _INFRA_ACTION_NEGATION_WORDS,
    _INFRA_ACTION_REGEXES,
    _INFRA_ACTION_VERBS,
    _INFRA_NOUN_ALT,
    _INFRA_TARGET_NOUNS,
    _INFRA_VERB_ALT,
    _LLM_TOKEN_METRIC_REGEXES,
    _PLACEHOLDER_PHRASES,
    _POLICY_RULE_PREFIXES,
    _REJECT_KEYWORDS,
    _SECRET_ACTION_MAX_FILLER_WORDS,
    _SECRET_ACTION_NOUNS,
    _SECRET_ACTION_REGEXES,
    _SECRET_ACTION_VERBS,
    _SECRET_NOUN_ALT,
    _SECRET_VERB_ALT,
    _check_goal_policy,
    _infra_action_match_is_false_positive,
    _is_code_or_test_file,
    _policy_rule_for_reason,
    _secret_action_match_is_false_positive,
    plan_violates_policy,
)
from .prompts import (
    _DIRECT_IMPLEMENTATION_PROMPT_TEMPLATE,
    _IMPLEMENTATION_PROMPT_TEMPLATE,
    _NEXUS_IDENTITY_PROMPT,
    _PLAN_PROMPT_TEMPLATE,
    _POLICY_REPLAN_PROMPT_TEMPLATE,
    _REFORMAT_RETRY_PROMPT_TEMPLATE,
    _RESEARCH_PROMPT_TEMPLATE,
    _build_direct_implementation_prompt,
    _build_implementation_prompt,
    _build_plan_prompt,
    _build_policy_replan_prompt,
    _build_reformat_retry_prompt,
    _build_research_prompt,
    _with_nexus_identity,
)
from .service import (
    _DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS,
    _DEFAULT_MAX_DIAGNOSTIC_LOG_BYTES,
    _DEFAULT_MAX_DIFF_BYTES,
    _DEFAULT_MAX_RAW_MESSAGE_BYTES,
    _DEFAULT_MAX_TEST_OUTPUT_BYTES,
    _DEFAULT_PLAN_TIMEOUT_SECONDS,
    _DEFAULT_RESEARCH_TIMEOUT_SECONDS,
    _DEFAULT_TEST_COMMAND_KEY,
    _DEFAULT_TEST_PYTHON_EXECUTABLE,
    _DEFAULT_TEST_TIMEOUT_SECONDS,
    _ERROR_STATUSES,
    _MAX_TEST_TIMEOUT_SECONDS,
    _REFORMAT_RETRY_TIMEOUT_SECONDS,
    _TEST_COMMANDS,
    _TEST_TIMEOUT_BACKOFF_FACTOR,
    _TEST_TIMEOUT_SETTING_KEY,
    _WARNING_STATUSES,
    ExperimentService,
    logger,
)
from .text_utils import (
    _DIAGNOSTIC_EVENT_TYPES,
    _DIAGNOSTIC_ITEM_TYPES,
    _DIAGNOSTIC_TOOL_ITEM_TYPES,
    _SECRET_PATTERNS,
    _extract_diagnostic_log,
    _redact_secrets,
    _truncate_text,
    _utcnow_iso,
)
