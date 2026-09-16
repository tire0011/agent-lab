"""Shared test setup.

This file runs before any test module is imported, which is the only point at
which these can still take effect: both settings are read into module-level
constants and bound as default arguments at import time.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Keep the suite out of the production log directory. `DEFAULT_LOG_DIR` is a
# relative path, so without this a test run writes into
# /agent/state/experiment-logs alongside real history - which is how test
# controller turns were first mistaken for production data while measuring
# escalation rates for #158.
_LOG_DIR = tempfile.mkdtemp(prefix="nexus-test-logs-")
os.environ.setdefault("AGENT_LOG_DIR", _LOG_DIR)

# api.py reads this at module level, and several service defaults resolve the
# database path at import time. Without it a suite run can touch the real
# state database.
os.environ.setdefault("AGENT_API_KEY", "test-api-key")

# Mailbox configuration is required at import time by api.py. Point it at an
# isolated canonical-layout checkout so tests can never fall back to /agent.
os.environ.setdefault("AGENT_MAILBOX_REPO_PATH", str(Path(_LOG_DIR) / "mailbox-repo"))

# The suite must never reach a real model provider. It could: the controller
# escalates to `codex_readonly` when a prompt exceeds the local budget, and on
# the VM the codex binary exists - so a test whose injected client was supposed
# to answer instead spent a real ~6s Codex call, returned a different answer,
# and failed. Worse, whether that happened depended on how much memory the host
# had free, so results varied between runs (#158).
#
# Assigned, not setdefault: the isolation must not depend on the variable
# happening to be unset. The service currently relies on the module default
# rather than setting it, but any shell or future deployment that does set it
# would otherwise hand the suite a working binary again. A test that needs a
# codex path sets it through monkeypatch, which applies after this file runs.
os.environ["AGENT_CODEX_BIN"] = str(Path(_LOG_DIR) / "no-codex-binary-in-tests")

# Same isolation for the second coding agent: the claude CLI is installed on
# the VM too, and a task whose provider was drawn at random would otherwise
# spend a real Claude call. With both binaries unreachable, the provider draw
# finds nothing installed and falls back to codex, which keeps the suite
# deterministic as well.
os.environ["AGENT_CLAUDE_BIN"] = str(Path(_LOG_DIR) / "no-claude-binary-in-tests")
