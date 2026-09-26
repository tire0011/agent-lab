"""Generic read-only diagnostics mailbox message."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from modules.nexus_diagnostics import DiagnosticsError, NexusDiagnostics


def _db_path(conn: Any) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    if not row or not row[2]:
        raise ValueError("diagnose requires a file-backed Nexus database")
    return str(row[2])


def process_diagnose(
    msg: Any,
    repo_path: Path,
    conn: Any,
    db_msg_id: int,
) -> tuple[str, None]:
    del repo_path, db_msg_id
    try:
        request = json.loads(msg.body or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"diagnose body must be JSON: {exc}") from exc
    if not isinstance(request, dict):
        raise ValueError("diagnose body must be a JSON object")

    reference = str(request.get("reference") or request.get("id") or "").strip()
    if not reference:
        raise ValueError("diagnose requires reference or id")

    subject = str(request.get("subject") or "auto").strip().lower()
    include = request.get("include") or []
    if isinstance(include, str):
        include = [include]
    if not isinstance(include, list) or not all(isinstance(item, str) for item in include):
        raise ValueError("diagnose include must be a string or list of strings")

    lines = request.get("lines")
    try:
        with NexusDiagnostics(db_path=_db_path(conn)) as diagnostics:
            payload = diagnostics.inspect(
                reference,
                subject=subject,
                include=include,
                lines=lines,
            )
    except DiagnosticsError as exc:
        raise ValueError(str(exc)) from exc

    return (
        "JSON diagnostic result:\n" + json.dumps(payload, ensure_ascii=False, indent=2),
        None,
    )
