"""Builds the verification environment for the verification test subprocesses started by
``ExperimentService._verify``. The subprocess used to run with ``env=None``, silently inheriting
the entire ``agent-api`` environment - including the production ``AGENT_API_KEY``, which made
freshly-imported ``api`` modules inside tests pick up the production key and fail auth tests with
unexpected 401s. This module is deliberately separate from the coding agents environment minimisation
(``modules.coding_worker.filtered_env``), which must keep passing the LLM provider credentials the
implementation phase needs; verification runs need none of those and must never see them."""

from __future__ import annotations

import os
import re
from typing import Mapping

_SENSITIVE_ENV_RE = re.compile(r"(?i)(key|secret|token|password|credential|authorization|openai|azure|codex|ssh|agent_mailbox)")

# The fixed test-only value the suite already expects (``os.environ.setdefault("AGENT_API_KEY",
# "test-key")`` throughout tests/*.py): verification subprocesses always get exactly this, never
# whatever real key the parent agent-api process holds.
VERIFICATION_TEST_API_KEY = "test-key"


def build_verification_test_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the isolated environment for one verification test subprocess: start from the parent
    environment, remove sensitive variables, then explicitly set ``AGENT_API_KEY`` to the fixed test
    value. No static allowlist, so no production API key, LLM provider credential, GitHub/Copilot token
    or other secret-shaped variable can reach the test process regardless of the parent. Values are
    never logged here or by the caller."""
    source: Mapping[str, str] = base_env if base_env is not None else os.environ
    env = {key: value for key, value in source.items() if not _SENSITIVE_ENV_RE.search(key)}
    env["AGENT_API_KEY"] = VERIFICATION_TEST_API_KEY
    return env
