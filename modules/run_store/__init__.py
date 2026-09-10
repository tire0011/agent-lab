"""SQLite run-store public API and backwards-compatible package façade.

Schema creation and additive migrations live in :mod:`.schema`; run, price,
activity, and settings helpers live in :mod:`.records`.
"""

from __future__ import annotations

import os
import sqlite3

from . import schema as _schema
from .records import (
    calculate_estimated_cost,
    get_applicable_price,
    get_setting,
    insert_activity,
    insert_run,
    insert_tool_calls,
    list_activity,
    set_setting,
)

DEFAULT_DB_PATH = os.environ.get("AGENT_DB_PATH", "/var/lib/nexus/agent.db")
LEGACY_DB_PATH = "/agent/state/agent.db"


def _sync_database_paths() -> None:
    """Keep mutable legacy module constants effective after the package split."""
    _schema.DEFAULT_DB_PATH = DEFAULT_DB_PATH
    _schema.LEGACY_DB_PATH = LEGACY_DB_PATH


def _connect(db_path: str) -> sqlite3.Connection:
    """Open a configured SQLite connection using the current package paths."""
    _sync_database_paths()
    return _schema._connect(db_path)


def init_db(db_path: str | None = None) -> sqlite3.Connection:
    """Open a database and apply the complete schema and additive migrations."""
    _sync_database_paths()
    conn = _schema.init_db(DEFAULT_DB_PATH if db_path is None else db_path)

    # GitHubWorkTrackingService owns the durable CI table, so the table may
    # not exist on the first init_db() during process construction. Once it
    # does exist, every later initialization opportunistically migrates
    # authoritative CI evidence left by pre-#139 experiment verification.
    # The migration is local-only and idempotent.
    from modules.ci_evidence_backfill import backfill_legacy_ci_evidence

    backfill_legacy_ci_evidence(conn)
    return conn


__all__ = [
    "DEFAULT_DB_PATH",
    "LEGACY_DB_PATH",
    "_connect",
    "calculate_estimated_cost",
    "get_applicable_price",
    "get_setting",
    "init_db",
    "insert_activity",
    "insert_run",
    "insert_tool_calls",
    "list_activity",
    "set_setting",
]
