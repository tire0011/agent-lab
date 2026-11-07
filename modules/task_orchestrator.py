"""Agentic local-LLM controller for free-form Nexus work."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError, extract_json
from modules.heartbeat import Heartbeat, worker_id as heartbeat_worker_id
from modules.local_llm_jobs import (
    LocalLlmJobError,
    LocalLlmJobService,
    acquire_local_llm_job_service,
    local_llm_watchdog_seconds,
    release_local_llm_job_service,
)
from modules.escalation_probes import (
    ProbeResult,
    issue_text,
    logprob_decision,
    memory_entry_text,
    same_requirement_logprob_prompt,
)
from modules.mailbox.development import process_command
from modules.nexus_delegation import NexusDelegationService
from modules.nexus_memory import NexusMemory
from modules.nexus_task_store import (
    STATUS_FAILED,
    TERMINAL_REASON_TURN_CANCELLED,
    TERMINAL_REASON_TURN_FAILED,
    NexusTaskStore,
)
from modules.llama_cpp_client import json_schema_response_format
from modules.nexus_tool_router import (
    ACTION_CALL_TOOL,
    ACTION_COMMAND,
    ACTION_MISSING_CAPABILITY,
    ACTION_RESOLVE_RELATED_OPERATION,
    ACTION_RESUME_EXPERIMENT,
    EXPERIMENT_ID_PATTERN,
    FIND_TOOL_GOAL_EXAMPLE,
    FIND_TOOL_GOAL_PLACEHOLDER,
    MAX_EXPERIMENT_ID_CANDIDATES,
    MAX_RESOLVER_CANDIDATES,
    RESUME_INTENT_PATTERN,
    TOOL_ACCURACY_CORRECT,
    TOOL_ACCURACY_INCORRECT,
    TOOL_ACCURACY_UNKNOWN,
    TOOL_FIND_TOOL,
    TOOL_PYTHON_RUN,
    TOOL_RESOLVER_RESPONSE_FORMAT,
    TOOL_SEARCH_STOP_WORDS,
    NexusToolRouter,
    ToolResolverUnavailableError,
    ToolRouterError,
    controller_native_capabilities,
    operation_already_resolved,
    related_operation_lookup_goal,
    terminal_not_found_guidance,
    terminal_not_found_observation,
    tokens as tool_search_tokens,
)
from modules.controller_observations import _args_signature, controller_observation_view
from modules.experiment_handover import experiment_handover
from modules.prompt_budget import (
    CHARS_PER_TOKEN,
    PromptBudgetExhaustedError,
    PromptBudgetPlan,
    compact_observations,
    compute_budget,
    dedupe_nested_results,
    fit_history,
    prompt_json,
    read_mem_available_kb,
    read_swap_free_kb,
    read_swap_total_kb,
    truncate_text,
)
from modules.redaction import redact_secrets
from modules.run_store import DEFAULT_DB_PATH
from modules.runtime_execution import (
    RuntimeExecutionError,
    build_python_command,
    classify_command_failure,
    corrected_command,
    exit_code_of,
    resolve_python_interpreter,
)
from modules.tool_loop_guard import PersistentToolLoopGuard
from modules.tool_result_reader import (
    TOOL_READ_TOOL_RESULT,
    duplicate_read_guidance,
    limit_cap as read_tool_result_limit_cap,
    normalize_arguments as read_tool_result_arguments,
    read_tool_result,
)


logger = logging.getLogger(__name__)


from modules.task_orchestrator_parts.core import (
    ACTION_ANSWER,
    ACTION_CLARIFICATION,
    ACTION_KNOWLEDGE_COLLECTOR,
    ACTION_NEW_EXPERIMENT,
    ControllerBudgetExceededError,
    TOOL_KNOWLEDGE_BRIEFING,
    TaskDecision,
    TaskOrchestratorError,
    _AZURE_CONTROLLER_MAX_TOKENS,
    _CONTROLLER_PROVIDER_ENV,
    _DECISION_FIELDS,
    _EXPERIMENT_ID_PATTERN,
    _EXTERNAL_FALLBACK_ORDER,
    _EXTERNAL_PREAMBLE_CHARS,
    _EXTERNAL_PROMPT_CHARS,
    _FINAL_ACTIONS,
    _GITHUB_WRITE_COMMAND,
    _HISTORY_HEADER,
    _KNOWLEDGE_COLLECTOR_RESPONSE_FORMAT,
    _KNOWLEDGE_COLLECTOR_SCHEMA,
    _KNOWLEDGE_LOOKUP_REASON,
    _MAX_EXPERIMENT_ID_CANDIDATES,
    _MAX_RESOLVER_CANDIDATES,
    _ORCHESTRATOR_RESPONSE_FORMAT,
    _ORCHESTRATOR_SCHEMA,
    _ORCHESTRATOR_TOOLS,
    _PROVIDER_FALLBACKS,
    _RESUME_INTENT_PATTERN,
    _SOLVE_ISSUE_RE,
    _TOOL_ACCURACY_CORRECT,
    _TOOL_ACCURACY_INCORRECT,
    _TOOL_ACCURACY_UNKNOWN,
    _TOOL_RESOLVER_RESPONSE_FORMAT,
    _TOOL_SEARCH_STOP_WORDS,
    _TaskStoreGrantLedger,
    _guidance_core,
    _issue_briefing_decision,
    _repeats_tool_guidance,
)
from modules.task_orchestrator_parts.session import SessionMixin
from modules.task_orchestrator_parts.controller import ControllerLoopMixin
from modules.task_orchestrator_parts.providers import ProviderToolMixin
from modules.task_orchestrator_parts.knowledge import KnowledgeCollectorMixin
from modules.task_orchestrator_parts.context import ContextMixin
from modules.task_orchestrator_parts.execution import ExecutionMixin


class NexusTaskOrchestrator(
    SessionMixin,
    ControllerLoopMixin,
    ProviderToolMixin,
    KnowledgeCollectorMixin,
    ContextMixin,
    ExecutionMixin,
):
    """Persistent local-LLM controller that chooses Nexus's next action."""
