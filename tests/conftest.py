"""Shared test setup.

This file runs before any test module is imported, which is the only point at
which these can still take effect: both settings are read into module-level
constants and bound as default arguments at import time.
"""

from __future__ import annotations

import os
import tempfile

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
