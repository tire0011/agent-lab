"""Builds the verification environment without a static allowlist for verification test
subprocesses started by ``ExperimentService._verify``.

Root cause this fixes: the verification test subprocess (``python -m
unittest discover -s tests -q`` inside the experiment worktree) previously ran
via ``subprocess.run(..., env=None)``, which means it silently inherited the
*entire* environment of the running ``agent-api`` process - including the
real, production ``AGENT_API_KEY``. Since ``api.py`` reads
``AGENT_API_KEY`` once at import time and the test suite expects
``AGENT_API_KEY=test-key`` (see the ``os.environ.setdefault(...)`` calls at
the top of ``tests/test_api.py`` and friends), a freshly-imported ``api``
module inside that inherited environment picked up the production key
instead, and every API-auth test that asserted a 200/401 based on the
*expected* test key failed unpredictably with unexpected 401s.

This module is deliberately separate from the coding agents' own
environment minimisation (``modules.coding_worker.filtered_env``, used by
``CodexWorker._safe_env`` and ``ClaudeWorker._safe_env``), which builds the
environment for the *implementation* phase and must keep passing through the
LLM provider credentials that phase actually needs (``OPENAI_API_KEY``,
``AZURE_OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``, ...). Verification test runs
need none of those, and must never see them.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Mapping

_SENSITIVE_ENV_RE = re.compile(r"(?i)(key|secret|token|password|credential|authorization|openai|azure|codex|ssh|agent_mailbox)")

# The fixed, test-only value the whole test suite already expects (see
# ``os.environ.setdefault("AGENT_API_KEY", "test-key")`` throughout
# tests/*.py). Verification subprocesses must always get exactly this
# value, never whatever real key the parent ``agent-api`` process holds.
VERIFICATION_TEST_API_KEY = "test-key"

# The clock-drift fixture (#687) switch. Only the timing check sets it, for
# its own pytest child; a verification run must never inherit it from its
# parent - e.g. a verification nested in a test that itself runs under the
# timing check (tests/test_issue_689_*) would otherwise run its "normal" test
# run under drift as well (#736).
CLOCK_DRIFT_ENV = "NEXUS_TEST_CLOCK_DRIFT_MS"

# Same value tests/conftest.py assigns: port 9 (discard) refuses at once.
UNREACHABLE_LOCAL_MODEL_URL = "http://127.0.0.1:9"
LOCAL_MODEL_URL_ENVS = ("LOCAL_MODEL_URL", "LLAMA_CPP_BASE_URL", "LLAMA_API_URL")


# Test runs leave temporary files behind (fixtures without cleanup, crashed
# runs). On the VM /tmp is only cleared at reboot, and 2026-11-22 it held 30,000
# leftover directories (7.3 GB, disk 93 % full). Each verification run gets its
# own TMPDIR below this parent; the tick prunes old run directories.
TEST_TMP_PARENT_NAME = "nexus-test-tmp"
TEST_TMP_MAX_AGE_SECONDS = 6 * 3600  # longer than any run's safety cap


def verification_tmp_parent() -> Path:
    return Path(tempfile.gettempdir()) / TEST_TMP_PARENT_NAME


def new_test_tmp_dir() -> Path:
    parent = verification_tmp_parent()
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=parent))


def prune_test_tmp(max_age_seconds: float = TEST_TMP_MAX_AGE_SECONDS,
                   now: float | None = None) -> int:
    """Remove run directories not modified for *max_age_seconds*; returns how
    many were removed. Best effort: a directory that cannot be removed stays."""
    parent = verification_tmp_parent()
    if not parent.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for child in parent.iterdir():
        try:
            if now - child.stat().st_mtime < max_age_seconds:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
            removed += not child.exists()
        except OSError:
            continue
    return removed


def build_verification_test_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the isolated environment for one verification test subprocess.

    Starts from the parent environment, removes sensitive variables,
    and then explicitly sets ``AGENT_API_KEY`` to the fixed test value. No static
    environment allowlist is used. As a result, no production
    API key, LLM provider credential, GitHub/Copilot token, authorization
    header, or other secret-shaped variable can ever reach the verification
    test process - regardless of what the parent process happens to have
    set. Values are never logged by this function or its caller.
    """
    source: Mapping[str, str] = base_env if base_env is not None else os.environ
    env = {key: value for key, value in source.items() if not _SENSITIVE_ENV_RE.search(key)}
    env.pop(CLOCK_DRIFT_ENV, None)
    env["AGENT_API_KEY"] = VERIFICATION_TEST_API_KEY
    run_tmp = new_test_tmp_dir()
    for name in ("TMPDIR", "TEMP", "TMP"):
        env[name] = str(run_tmp)
    # The service environment exports ``AGENT_DB_PATH`` pointing at the
    # production database (nexus-tick.service sets it), and the verification
    # suite also runs via ``python -m unittest discover`` - where
    # ``tests/conftest.py`` does not execute. Without this, ``DEFAULT_DB_PATH``
    # binds to the production database at import time and any test using a
    # default path opens and locks it while the API service is writing; a
    # suite run inside the tick hung for hours on exactly this (#766).
    # Under pytest, conftest.py assigns its own temporary path anyway.
    if "AGENT_DB_PATH" in source:
        env["AGENT_DB_PATH"] = str(
            Path(tempfile.mkdtemp(prefix="nexus-verification-db-", dir=run_tmp)) / "agent.db"
        )
    # Same reason, and the actual cause of the 2026-11-19 tick hang (#766):
    # under unittest discovery conftest.py does not redirect the local model,
    # so a default-constructed LlamaCppClient talks to the real llama-server
    # (it falls back to 127.0.0.1:8080 even when no variable is set). A
    # mailbox test's background failure analysis queued a real inference
    # there, and LocalLlmJobService.close() finishes an in-flight job before
    # returning - minutes at ~3 tok/s, behind Nexus's own controller jobs.
    # Assigned unconditionally, like conftest.py: connection refused is
    # immediate, and a test that needs a model server sets its own URL.
    for name in LOCAL_MODEL_URL_ENVS:
        env[name] = UNREACHABLE_LOCAL_MODEL_URL
    return env
