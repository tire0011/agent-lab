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
from typing import Mapping

_SENSITIVE_ENV_RE = re.compile(r"(?i)(key|secret|token|password|credential|authorization|openai|azure|codex|ssh|agent_mailbox)")

# The fixed, test-only value the whole test suite already expects (see
# ``os.environ.setdefault("AGENT_API_KEY", "test-key")`` throughout
# tests/*.py). Verification subprocesses must always get exactly this
# value, never whatever real key the parent ``agent-api`` process holds.
VERIFICATION_TEST_API_KEY = "test-key"
_TIMING_CHECK_DRIFT_ENV_VAR = "NEXUS_TEST_CLOCK_DRIFT_MS"


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
    # This is a test-harness control rather than ordinary test configuration.
    # In particular, a timing check can run tests which themselves start a
    # normal verification subprocess.  Letting the outer drift value leak into
    # that nested subprocess makes its baseline test run drifted too and can
    # prevent the timing-check code under test from running at all.  The timing
    # check adds its own requested value after building this clean environment.
    env.pop(_TIMING_CHECK_DRIFT_ENV_VAR, None)
    env["AGENT_API_KEY"] = VERIFICATION_TEST_API_KEY
    return env
