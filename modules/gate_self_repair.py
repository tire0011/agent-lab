"""#730: gate self-repair - detect a repeated host blocker and switch one gate off.

When the last three finished experiments, on three different GitHub issues, all
end with the same normalized gate reason (the ``timing check failed closed:
... without drift too`` prefix), the gate is rerun once on current ``main``
with the same test ids in the verification environment. If it fails there too,
the blocker is the host, not the changes: the gate is classified
``host_blocker:timing_check`` and switched off persistently in the settings DB,
so :func:`modules.experiment_service.service_parts.verification.timing_check_enabled`
honours it across restarts.

Only ``timing_check`` is allowlisted for switch-off; review, the test suite, the
main guard and the safeguards can never be switched off, and at most one gate
is off at a time. A repair issue titled ``Repair host blocker: timing_check``
is opened *without* the ``proposal`` label (never via ``open_need_issue`` with
its proposal footer) and its number is stored with the switch-off. GitHub and
mailbox reporting go through a durable pending row (``gate_repair_reports``)
that is retried on the next tick, so a failed delivery never loses the report.
The state is exposed via :func:`status` (``/status`` and the status issue).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

GATE_TIMING_CHECK = "timing_check"
# The only gates that may ever be switched off (#730 allowlist).
ALLOWED_SWITCH_OFF_GATES = frozenset({GATE_TIMING_CHECK})
# Gates that must never be switched off, whatever the detection says.
PROTECTED_GATES = ("review", "test_suite", "main_guard", "safeguards")
HOST_BLOCKER_PREFIX = "host_blocker:"

GATE_REASON_PREFIX = "timing check failed closed:"
WITHOUT_DRIFT_MARK = "without drift too"
REQUIRED_SAME_REASON_RUNS = 3

SETTING_DISABLED_GATES = "gate_self_repair.disabled_gates"
SETTING_STATE = "gate_self_repair.state"
REPORT_TABLE = "gate_repair_reports"
REPAIR_ISSUE_TITLE = f"Repair host blocker: {GATE_TIMING_CHECK}"

REPORT_KIND_GITHUB_ISSUE = "github_issue"
REPORT_KIND_MAILBOX = "mailbox"

REPORT_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {REPORT_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('{REPORT_KIND_GITHUB_ISSUE}', '{REPORT_KIND_MAILBOX}')),
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NULL,
    result_json TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gate_repair_reports_status
    ON {REPORT_TABLE}(status, id);
"""

_ISSUE_RE = re.compile(r"\bGitHub\s+Issue\s+#(\d+)\b", re.IGNORECASE)
_TEST_ID_RE = re.compile(r"[\w./-]+(?:::[^\s,]+)+")
_SETTINGS_UPSERT = (
    "INSERT INTO service_settings (key, value, updated_at) VALUES (?, ?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at"
)

Deliver = Callable[[str, dict[str, Any]], dict[str, Any]]
Runner = Callable[[str, list[str]], str]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_db_path() -> str:
    return os.environ.get("AGENT_DB_PATH", "/var/lib/nexus/agent.db")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the settings table and the durable pending-report table if missing.

    Idempotent; also used on connections that did not come from
    :func:`modules.run_store.init_db`.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS service_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
        + REPORT_SCHEMA_SQL
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Gate reason normalization
# --------------------------------------------------------------------------- #

def normalize_gate_reason(error: str | None) -> str | None:
    """The normalized gate reason of a finished experiment's error, or ``None``.

    Only timing-check failures that failed closed because tests fail on this
    host without drift too count (the ``timing check failed closed: ...
    without drift too`` prefix). The variable test-id list after the mark is
    cut off, so two failures naming different tests normalize identically.
    """
    text = str(error or "")
    if not text.startswith(GATE_REASON_PREFIX) or WITHOUT_DRIFT_MARK not in text:
        return None
    cut = text.index(WITHOUT_DRIFT_MARK) + len(WITHOUT_DRIFT_MARK)
    return text[:cut]


def extract_test_ids(error: str | None) -> list[str]:
    """The test ids named in a ``... without drift too: <ids>`` gate error."""
    text = str(error or "")
    if WITHOUT_DRIFT_MARK not in text:
        return []
    tail = text.split(WITHOUT_DRIFT_MARK, 1)[1]
    return sorted(set(_TEST_ID_RE.findall(tail)))


# --------------------------------------------------------------------------- #
# Detection over the stored experiment rows
# --------------------------------------------------------------------------- #

def _terminal_statuses() -> tuple[str, ...]:
    from modules.experiment_service.models import _TERMINAL_STATUSES

    return tuple(_TERMINAL_STATUSES)


def _rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any]) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, tuple(params))
    cursor.row_factory = sqlite3.Row
    return [dict(row) for row in cursor.fetchall()]


def recent_finished(conn: sqlite3.Connection, limit: int = REQUIRED_SAME_REASON_RUNS) -> list[dict[str, Any]]:
    """The newest finished (terminal) experiments, newest first."""
    statuses = _terminal_statuses()
    placeholders = ", ".join("?" for _ in statuses)
    try:
        return _rows(
            conn,
            "SELECT id, status, ended_at, error, goal FROM experiments "
            f"WHERE ended_at IS NOT NULL AND status IN ({placeholders}) "
            "ORDER BY ended_at DESC, id DESC LIMIT ?",
            (*statuses, max(1, limit)),
        )
    except sqlite3.OperationalError:
        return []


def issue_number_of(conn: sqlite3.Connection, experiment_row: dict[str, Any]) -> int | None:
    """The experiment's GitHub issue: the link table, else the goal text."""
    try:
        found = conn.execute(
            "SELECT issue_number FROM github_experiment_links WHERE experiment_id=?",
            (experiment_row["id"],),
        ).fetchone()
        if found is not None:
            return int(found[0])
    except sqlite3.OperationalError:
        pass
    match = _ISSUE_RE.search(str(experiment_row.get("goal") or ""))
    return int(match.group(1)) if match else None


def detect_repeated_blocker(
    conn: sqlite3.Connection, *, needed: int = REQUIRED_SAME_REASON_RUNS,
) -> dict[str, Any] | None:
    """The repeated host blocker, when the last *needed* finished experiments
    on *needed* different issues all end with the same normalized gate reason.

    Two failures, mixed reasons, or the same issue three times give ``None``.
    """
    rows = recent_finished(conn, needed)
    if len(rows) < needed:
        return None
    reasons = [normalize_gate_reason(row.get("error")) for row in rows]
    if any(reason is None for reason in reasons) or len(set(reasons)) != 1:
        return None
    issues = [issue_number_of(conn, row) for row in rows]
    if any(issue is None for issue in issues) or len(set(issues)) != needed:
        return None
    test_ids = sorted({test for row in rows for test in extract_test_ids(row.get("error"))})
    return {
        "reason": reasons[0],
        "experiments": [row["id"] for row in rows],
        "issues": issues,
        "test_ids": test_ids,
    }


# --------------------------------------------------------------------------- #
# Rerun the gate once on current main
# --------------------------------------------------------------------------- #

@contextmanager
def _main_worktree(repo_root: str, sha: str) -> Iterator[Path]:
    """A detached temporary worktree at *sha*, removed afterwards."""
    parent = Path(tempfile.mkdtemp(prefix="nexus-gate-repair-"))
    worktree = parent / "main"
    add = subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), sha],
        cwd=repo_root, capture_output=True, text=True, timeout=120, check=False,
    )
    if add.returncode != 0:
        shutil.rmtree(parent, ignore_errors=True)
        raise RuntimeError(add.stderr.strip()[:400] or "git worktree add failed")
    try:
        yield worktree
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=repo_root, capture_output=True, text=True, timeout=120, check=False,
        )
        shutil.rmtree(parent, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=repo_root, capture_output=True, text=True, timeout=60, check=False,
        )


def rerun_gate_on_main(
    repo_root: str,
    test_ids: list[str],
    *,
    runner: Runner | None = None,
    python: str | None = None,
) -> dict[str, Any]:
    """Rerun the timing gate once on current ``main``: same test ids, verification env.

    The default runner is :func:`modules.timing_check.run_timing_check` with the
    drift environment, which runs its pytest child inside
    :func:`modules.verification_env.verification_test_env`. ``passed`` is
    ``result == "ok"``; anything else means the gate fails on main too.
    """
    from modules import timing_check as timing_check_module
    from modules.experiment_service.reverify import current_main

    main_sha = current_main(str(repo_root))
    if not test_ids:
        return {"main_sha": main_sha, "result": "ok", "passed": True, "test_ids": []}
    if runner is None:
        def runner(repo: str, tests: list[str]) -> str:
            return timing_check_module.run_timing_check(
                repo, tests, env=timing_check_module.drift_env(), python=python,
            )
    with _main_worktree(str(repo_root), main_sha) as worktree:
        result = runner(str(worktree), list(test_ids))
    return {"main_sha": main_sha, "result": result, "passed": result == "ok", "test_ids": list(test_ids)}


# --------------------------------------------------------------------------- #
# Persisted switch-off (settings DB)
# --------------------------------------------------------------------------- #

def _read_json_setting(conn: sqlite3.Connection, key: str) -> Any:
    try:
        row = conn.execute("SELECT value FROM service_settings WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def disabled_gates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The persisted list of switched-off gate entries, oldest first."""
    raw = _read_json_setting(conn, SETTING_DISABLED_GATES)
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict) and entry.get("gate")]


def gate_disabled(conn: sqlite3.Connection, gate: str) -> dict[str, Any] | None:
    """The persisted switch-off entry for *gate*, or ``None`` when it is on."""
    for entry in disabled_gates(conn):
        if entry.get("gate") == gate:
            return entry
    return None


def gate_disabled_at(db_path: str | None, gate: str) -> dict[str, Any] | None:
    """Read the persisted switch-off straight from *db_path* (no live service).

    A missing database or table means "not disabled"; a locked database must
    not crash verification, it just falls back to the environment override.
    """
    if not db_path or db_path == ":memory:":
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        return gate_disabled(conn, gate)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def disable_gate(
    conn: sqlite3.Connection,
    gate: str,
    *,
    classification: str,
    reason: str,
    test_ids: Iterable[str] = (),
    experiments: Iterable[str] = (),
    issues: Iterable[int] = (),
    main_sha: str | None = None,
    rerun_result: str | None = None,
    repair_issue_number: int | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Persist the switch-off of *gate* in the settings DB (survives restarts).

    Only allowlisted gates may be switched off (``timing_check`` only), and at
    most one gate may be off at a time; anything else raises ``ValueError``
    and changes nothing. The repair issue number is stored with the switch-off.
    """
    if gate not in ALLOWED_SWITCH_OFF_GATES:
        raise ValueError(
            f"gate {gate!r} is not allowlisted for switch-off "
            f"(allowlist: {sorted(ALLOWED_SWITCH_OFF_GATES)}; protected: {list(PROTECTED_GATES)})"
        )
    now = now or _utcnow()
    current = disabled_gates(conn)
    if current and all(entry.get("gate") != gate for entry in current):
        raise ValueError(
            "at most one gate can be switched off at a time; already off: "
            + ", ".join(str(entry.get("gate")) for entry in current)
        )
    entry = {
        "gate": gate,
        "classification": classification,
        "reason": reason,
        "test_ids": list(test_ids),
        "experiments": list(experiments),
        "issues": list(issues),
        "main_sha": main_sha,
        "rerun_result": rerun_result,
        "repair_issue_number": repair_issue_number,
        "disabled_at": now,
        "updated_at": now,
    }
    entries = [e for e in current if e.get("gate") != gate] + [entry]
    with conn:
        conn.execute(_SETTINGS_UPSERT, (SETTING_DISABLED_GATES, json.dumps(entries), now))
        conn.execute(_SETTINGS_UPSERT, (SETTING_STATE, json.dumps(entry), now))
    return entry


def set_repair_issue_number(
    conn: sqlite3.Connection, gate: str, issue_number: int, now: str | None = None,
) -> dict[str, Any] | None:
    """Store the opened repair issue's number with the switch-off."""
    now = now or _utcnow()
    entry = gate_disabled(conn, gate)
    if entry is None:
        return None
    entry["repair_issue_number"] = int(issue_number)
    entry["updated_at"] = now
    entries = [e for e in disabled_gates(conn) if e.get("gate") != gate] + [entry]
    with conn:
        conn.execute(_SETTINGS_UPSERT, (SETTING_DISABLED_GATES, json.dumps(entries), now))
        conn.execute(_SETTINGS_UPSERT, (SETTING_STATE, json.dumps(entry), now))
    return entry


def gate_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """The current gate self-repair state (for ``status`` and the status issue)."""
    entries = disabled_gates(conn)
    latest = _read_json_setting(conn, SETTING_STATE)
    if not isinstance(latest, dict):
        latest = entries[-1] if entries else None
    pending = 0
    try:
        pending = int(conn.execute(
            f"SELECT COUNT(*) FROM {REPORT_TABLE} WHERE status='pending'"
        ).fetchone()[0])
    except sqlite3.Error:
        pass
    timing_entry = gate_disabled(conn, GATE_TIMING_CHECK)
    return {
        "disabled_gates": [entry.get("gate") for entry in entries],
        "timing_check": "off" if timing_entry is not None else "on",
        "host_blocker": (latest or {}).get("classification"),
        "repair_issue_number": (latest or {}).get("repair_issue_number"),
        "state": latest,
        "pending_reports": pending,
    }


def status(db_path: str | None = None) -> dict[str, Any]:
    """The gate self-repair state read from *db_path* (default: the configured DB)."""
    path = db_path or _default_db_path()
    fallback = {
        "disabled_gates": [], "timing_check": "on", "host_blocker": None,
        "repair_issue_number": None, "state": None, "pending_reports": 0,
    }
    if path == ":memory:" or not os.path.exists(path):
        return fallback
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return fallback
    try:
        return gate_state(conn)
    except sqlite3.Error:
        return fallback
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Durable pending reports (GitHub / mailbox), retried on the next tick
# --------------------------------------------------------------------------- #

def enqueue_report(conn: sqlite3.Connection, kind: str, payload: dict[str, Any], now: str | None = None) -> int:
    """Insert a durable pending report row; returns its id."""
    now = now or _utcnow()
    with conn:
        cursor = conn.execute(
            f"INSERT INTO {REPORT_TABLE} (kind, payload_json, status, attempts, created_at, updated_at) "
            "VALUES (?, ?, 'pending', 0, ?, ?)",
            (kind, json.dumps(payload), now, now),
        )
    return int(cursor.lastrowid)


def pending_reports(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        return _rows(conn, f"SELECT * FROM {REPORT_TABLE} WHERE status='pending' ORDER BY id", ())
    except sqlite3.OperationalError:
        return []


def retry_pending_reports(conn: sqlite3.Connection, deliver: Deliver, *, now: str | None = None) -> list[dict[str, Any]]:
    """Deliver every pending report row once; failures stay pending for the next tick.

    A successful ``github_issue`` delivery stores the opened issue's number
    with the switch-off.
    """
    now = now or _utcnow()
    results: list[dict[str, Any]] = []
    for row in pending_reports(conn):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError) as exc:
            payload = {}
            result = {"error": f"unreadable payload: {exc}"}
        else:
            try:
                result = deliver(row["kind"], payload)
            except Exception as exc:  # noqa: BLE001 - a failed delivery is retried, never lost
                with conn:
                    conn.execute(
                        f"UPDATE {REPORT_TABLE} SET attempts=attempts+1, last_error=?, updated_at=? WHERE id=?",
                        (f"{type(exc).__name__}: {str(exc)[:500]}", now, row["id"]),
                    )
                results.append({
                    "id": row["id"], "kind": row["kind"], "status": "pending",
                    "attempts": row["attempts"] + 1, "error": str(exc)[:500],
                })
                continue
        with conn:
            conn.execute(
                f"UPDATE {REPORT_TABLE} SET status='done', attempts=attempts+1, result_json=?, last_error=NULL, updated_at=? WHERE id=?",
                (json.dumps(result), now, row["id"]),
            )
        if row["kind"] == REPORT_KIND_GITHUB_ISSUE and isinstance(result, dict):
            number = result.get("number")
            if number is not None:
                set_repair_issue_number(conn, GATE_TIMING_CHECK, number, now=now)
        results.append({"id": row["id"], "kind": row["kind"], "status": "done", "result": result})
    return results


def _gh_create_issue(repo_root: str, title: str, body: str) -> dict[str, Any]:
    """Open a plain issue via ``gh`` - deliberately *no* ``--label``, so the
    repair issue never carries the ``proposal`` label (and never goes through
    ``open_need_issue`` with its proposal footer)."""
    proc = subprocess.run(
        ["gh", "issue", "create", "--title", title, "--body", body],
        cwd=repo_root, capture_output=True, text=True, timeout=60, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:400] or f"gh exited {proc.returncode}")
    match = re.search(r"/issues/(\d+)", proc.stdout or "")
    return {"number": int(match.group(1)) if match else None, "url": (proc.stdout or "").strip()}


def _mailbox_post(payload: dict[str, Any]) -> dict[str, Any]:
    """Write the report into the mailbox repository's outbox."""
    import uuid

    from modules.mailbox.config import MailboxConfig

    config = MailboxConfig.from_env()  # raises when AGENT_MAILBOX_REPO_PATH is unset
    config.outbox_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utcnow().replace(":", "").replace("-", "").replace("T", "").replace("Z", "")[:15]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:8]
    name = f"{stamp}-gate-repair-{digest}.md"
    content = "\n".join([
        "---",
        "type: report",
        "status: sent",
        f"processed_at: {_utcnow()}",
        "created_by: nexus",
        "---",
        "",
        str(payload.get("body") or ""),
    ]) + "\n"
    path = config.outbox_dir / name
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return {"outbox_file": path.name}


def default_deliver(repo_root: str) -> Deliver:
    """The production delivery: a plain GitHub issue plus a mailbox outbox message."""
    def deliver(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind == REPORT_KIND_GITHUB_ISSUE:
            return _gh_create_issue(repo_root, str(payload["title"]), str(payload.get("body") or ""))
        if kind == REPORT_KIND_MAILBOX:
            return _mailbox_post(payload)
        raise ValueError(f"unknown report kind {kind!r}")
    return deliver


def _report_body(detection: dict[str, Any], rerun: dict[str, Any], entry: dict[str, Any]) -> str:
    issues = ", ".join(f"#{issue}" for issue in detection["issues"])
    tests = ", ".join(f"`{test}`" for test in detection["test_ids"]) or "none recorded"
    return "\n".join([
        "Nexus detected a repeated host-level blocker and switched one gate off (#730).",
        "",
        f"- Classification: `{entry['classification']}`",
        f"- Gate: `{entry['gate']}` (allowlisted; review, the test suite, the main guard and the safeguards are never switched off, and at most one gate is off at a time)",
        f"- Normalized gate reason: `{entry['reason']}`",
        f"- Last finished experiments: {', '.join(entry['experiments'])} on issues {issues}",
        f"- Affected test ids: {tests}",
        f"- Gate rerun on current main (`{entry.get('main_sha')}`) with the same test ids in the verification environment: `{entry.get('rerun_result')}`",
        "",
        "The timing check is now off persistently (settings DB, key "
        f"`{SETTING_DISABLED_GATES}`); `timing_check_enabled()` honours it across restarts.",
        f"To re-enable it, set that key back to `[]` in `service_settings`.",
    ])


# --------------------------------------------------------------------------- #
# Main entry: run once per tick
# --------------------------------------------------------------------------- #

def check_and_repair(
    *,
    db_path: str | None = None,
    repo_root: str,
    deliver: Deliver | None = None,
    runner: Runner | None = None,
    python: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Detect a repeated host blocker and switch the allowlisted gate off.

    Called once per tick. Pending reports from earlier ticks are retried first,
    so a failed GitHub/mailbox delivery is never lost. Returns the resulting
    :func:`gate_state` plus what happened (``switched_off``, ``detected``,
    ``rerun``, ``reports``).
    """
    now = now or _utcnow()
    path = db_path or _default_db_path()
    if path != ":memory:" and not os.path.exists(path):
        return {"switched_off": False, "detected": None, **status(path)}
    conn = sqlite3.connect(path, timeout=30)
    try:
        ensure_schema(conn)
        deliver = deliver if deliver is not None else default_deliver(repo_root)
        retried = retry_pending_reports(conn, deliver, now=now)
        state = gate_state(conn)
        if gate_disabled(conn, GATE_TIMING_CHECK) is not None:
            return {**state, "switched_off": False, "already_off": True, "retried_reports": retried}
        detection = detect_repeated_blocker(conn)
        if detection is None:
            return {**state, "switched_off": False, "detected": None, "retried_reports": retried}
        rerun = rerun_gate_on_main(repo_root, detection["test_ids"], runner=runner, python=python)
        if rerun["passed"]:
            return {
                **state, "switched_off": False, "detected": detection, "rerun": rerun,
                "retried_reports": retried,
            }
        classification = f"{HOST_BLOCKER_PREFIX}{GATE_TIMING_CHECK}"
        entry = disable_gate(
            conn, GATE_TIMING_CHECK,
            classification=classification,
            reason=detection["reason"],
            test_ids=detection["test_ids"],
            experiments=detection["experiments"],
            issues=detection["issues"],
            main_sha=rerun.get("main_sha"),
            rerun_result=rerun.get("result"),
            now=now,
        )
        body = _report_body(detection, rerun, entry)
        # Durable pending rows first: even if delivery fails right now, the
        # next tick retries them.
        enqueue_report(conn, REPORT_KIND_GITHUB_ISSUE, {"title": REPAIR_ISSUE_TITLE, "body": body}, now=now)
        enqueue_report(conn, REPORT_KIND_MAILBOX, {"title": REPAIR_ISSUE_TITLE, "body": body}, now=now)
        delivered = retry_pending_reports(conn, deliver, now=now)
        state = gate_state(conn)
        return {
            **state, "switched_off": True, "blocker": classification,
            "detected": detection, "rerun": rerun, "reports": delivered, "entry": entry,
        }
    finally:
        conn.close()
