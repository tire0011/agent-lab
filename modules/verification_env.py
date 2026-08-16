"""Builds the minimal, allowlisted environment for verification test
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

This module is deliberately separate from
``modules.codex_worker.CodexWorker._safe_env``, which builds the
environment for the Codex/Copilot *implementation* phase and must keep
passing through the LLM provider credentials that phase actually needs
(``OPENAI_API_KEY``, ``AZURE_OPENAI_API_KEY``, ...). Verification test runs
need none of those, and must never see them.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

# Runtime plumbing only: just enough for Python/unittest to start, locate
# the interpreter and any already-installed packages, and use a
# well-defined locale/home/temp-directory. None of these are provider
# credentials, tokens, or application secrets.
SAFE_RUNTIME_PASSTHROUGH: frozenset[str] = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL",
    "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "TEMP", "TMP",
    "PYTHONIOENCODING", "PYTHONUTF8",
})

# The fixed, test-only value the whole test suite already expects (see
# ``os.environ.setdefault("AGENT_API_KEY", "test-key")`` throughout
# tests/*.py). Verification subprocesses must always get exactly this
# value, never whatever real key the parent ``agent-api`` process holds.
VERIFICATION_TEST_API_KEY = "test-key"


def build_verification_test_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the isolated environment for one verification test subprocess.

    Starts from an *empty* environment (never a copy of the full parent
    environment), copies over only the small safe-runtime allowlist above
    from *base_env* (``os.environ`` by default), and then explicitly sets
    ``AGENT_API_KEY`` to the fixed test value. As a result, no production
    API key, LLM provider credential, GitHub/Copilot token, authorization
    header, or other secret-shaped variable can ever reach the verification
    test process - regardless of what the parent process happens to have
    set. Values are never logged by this function or its caller.
    """
    source: Mapping[str, str] = base_env if base_env is not None else os.environ
    env = {key: source[key] for key in SAFE_RUNTIME_PASSTHROUGH if key in source}
    env["AGENT_API_KEY"] = VERIFICATION_TEST_API_KEY
    return env


@contextmanager
def verification_test_env(
    base_env: Mapping[str, str] | None = None,
) -> Iterator[dict[str, str]]:
    """Yield an isolated verification environment with a temporary database.

    The environment retains the same allowlist and fixed test API key as
    :func:`build_verification_test_env`. Each context gets its own temporary
    directory and an explicit ``AGENT_DB_PATH`` within it. The temporary
    directory is removed when the context exits, including when its body
    raises an exception.
    """
    env = build_verification_test_env(base_env)
    with tempfile.TemporaryDirectory(prefix="nexus-verification-") as temp_dir:
        env["AGENT_DB_PATH"] = str(Path(temp_dir) / "agent.db")
        yield env
