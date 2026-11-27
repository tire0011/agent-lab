"""#730: gate self-repair - detect a repeated host blocker and switch one gate off.

When the last three finished experiments, on three different GitHub issues, all
end with the same normalized gate reason (the ``timing check failed closed:
... without drift too`` prefix), the *same* gate is rerun once on current
``main``: the drift run on the same test ids in the verification environment,
then - exactly as :meth:`VerificationMixin._run_timing_check` does - the
control run without drift on the flagged tests. Only when that rerun ends with
the same normalized reason (the tests fail on main without drift too) is the
blocker the host, not the changes: the gate is classified
``host_blocker:timing_check`` and switched off persistently in the settings
DB, so :func:`modules.experiment_service.service_parts.verification.timing_check_enabled`
honours it across restarts. A rerun that passes, that finds the tests really
timing-sensitive, or that gives no verdict (crash, watchdog, missing tests,
worktree trouble) switches nothing off.

The rerun happens at most once per detected experiment triple: its claim and
its outcome are persisted in ``service_settings`` keyed by the experiment ids,
so later ticks never repeat it (an interrupted rerun counts as no verdict).

Only ``timing_check`` is allowlisted for switch-off; review, the test suite, the
main guard and the safeguards can never be switched off, and at most one gate
is off at a time. A repair issue titled ``Repair host blocker: timing_check``
is opened *without* the ``proposal`` label (never via ``open_need_issue`` with
its proposal footer) and its number is stored with the switch-off. The
switch-off and its two durable pending report rows (GitHub issue, mailbox) are
written in one transaction; pending rows are retried on every tick, and a
switch-off found without its report rows gets them re-enqueued, so a failed
or interrupted delivery never loses the report. Delivery is idempotent (an
open issue with the same title is reused, the mailbox file name is fixed), so
a crash between delivering and marking a row done does not duplicate it. The
state is exposed via :func:`status` (``/status`` and the status issue).
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
import uuid
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
# The gate's own wording when the flagged tests fail without drift too
# (verification.py, ``_run_timing_check``).
FAILS_WITHOUT_DRIFT_RESULT = (
    "error: these tests fail on this host without drift too, so there is "
    "no timing verdict: "
)

SETTING_DISABLED_GATES = "gate_self_repair.disabled_gates"
SETTING_STATE = "gate_self_repair.state"
SETTING_RERUN_PREFIX = "gate_self_repair.rerun:"
SETTING_LAST_RERUN = "gate_self_repair.last_rerun"
REPORT_TABLE = "gate_repair_reports"
REPAIR_ISSUE_TITLE = f"Repair host blocker: {GATE_TIMING_CHECK}"

REPORT_KIND_GITHUB_ISSUE = "github_issue"
REPORT_KIND_MAILBOX = "mailbox"
REPORT_KINDS = (REPORT_KIND_GITHUB_ISSUE, REPORT_KIND_MAILBOX)

# Rerun verdicts. Only VERDICT_FAILED switches the gate off.
VERDICT_PASSED = "passed"
VERDICT_FAILED = "failed"
VERDICT_TIMING_SENSITIVE = "timing_sensitive"
VERDICT_NO_VERDICT = "no_verdict"
RERUN_RUNNING = "running"
RERUN_DONE = "done"

REPORT_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {REPORT_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('{REPORT_KIND_GITHUB_ISSUE}', '{REPORT_KIND_MAILBOX}')),
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NULL,
    result_json TEXT NULL,
    report_key TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gate_repair_reports_status
    ON {REPORT_TABLE}(status, id);
"""
# Separate, because a table created before report_key existed gets the
# column via ALTER TABLE first (see ensure_report_schema).
REPORT_KEY_INDEX_SQL = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS idx_gate_repair_reports_key "
    f"ON {REPORT_TABLE}(report_key)"
)

_ISSUE_RE = re.compile(r"\bGitHub\s+Issue\s+#(\d+)\b", re.IGNORECASE)
_TEST_ID_RE = re.compile(r"[\w./-]+(?:::[^\s,]+)+")
_SETTINGS_UPSERT = (
    "INSERT INTO service_settings (key, value, updated_at) VALUES (?, ?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at"
)

Deliver = Callable[[str, dict[str, Any]], dict[str, Any]]
# (worktree on main, test ids) -> the gate's result string, in the same form
# ``_run_timing_check`` builds it before the ``timing check failed closed: ``
# prefix: "ok", "timing_sensitive: ...", FAILS_WITHOUT_DRIFT_RESULT + ids, or
# "error: ...".
Runner = Callable[[str, list[str]], str]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_db_path() -> str:
    return os.environ.get("AGENT_DB_PATH", "/var/lib/nexus/agent.db")


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """One write transaction: everything inside commits together or not at all.

    ``BEGIN IMMEDIATE`` takes the write lock up front, so reads done inside
    (the at-most-one check) cannot race another writer. Callers inside must
    not commit themselves.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def ensure_report_schema(conn: sqlite3.Connection) -> None:
    """Create the pending-report table, adding ``report_key`` to an older one.

    Does not commit; :func:`modules.run_store.init_db` commits its migrations.
    """
    conn.executescript(REPORT_SCHEMA_SQL)
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({REPORT_TABLE})")}
    if "report_key" not in columns:
        conn.execute(f"ALTER TABLE {REPORT_TABLE} ADD COLUMN report_key TEXT NULL")
    conn.execute(REPORT_KEY_INDEX_SQL)


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
    )
    ensure_report_schema(conn)
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
    names = [column[0] for column in cursor.description or ()]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


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
    experiments = [row["id"] for row in rows]
    return {
        "reason": reasons[0],
        "experiments": experiments,
        "issues": issues,
        "test_ids": test_ids,
        "key": detection_key(experiments),
    }


def detection_key(experiments: Iterable[str]) -> str:
    """The settings key under which the rerun of this experiment triple is kept."""
    return SETTING_RERUN_PREFIX + ",".join(sorted(str(e) for e in experiments))


# --------------------------------------------------------------------------- #
# Rerun the same gate once on current main
# --------------------------------------------------------------------------- #

@contextmanager
def _main_worktree(repo_root: str, sha: str) -> Iterator[Path]:
    """A detached temporary worktree at *sha*, removed afterwards."""
    parent = Path(tempfile.mkdtemp(prefix="nexus-gate-repair-"))
    worktree = parent / "main"
    try:
        add = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), sha],
            cwd=repo_root, capture_output=True, text=True, timeout=120, check=False,
        )
    except BaseException:
        shutil.rmtree(parent, ignore_errors=True)
        raise
    if add.returncode != 0:
        shutil.rmtree(parent, ignore_errors=True)
        raise RuntimeError(add.stderr.strip()[:400] or "git worktree add failed")
    try:
        yield worktree
    finally:
        _quiet_git(repo_root, ["worktree", "remove", "--force", str(worktree)], 120)
        shutil.rmtree(parent, ignore_errors=True)
        # After the directory is gone, so stale metadata is cleared too.
        _quiet_git(repo_root, ["worktree", "prune"], 60)


def _quiet_git(repo_root: str, args: list[str], timeout: int) -> None:
    try:
        subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def default_gate_runner(python: str | None = None, watchdog: Any | None = None) -> Runner:
    """The timing gate exactly as verification runs it, for a worktree on main.

    Drift run (``drift_env()``: the verification environment plus the drift)
    on the test ids that exist on main; on flagged tests the control run
    without drift (``control_check``). The result string is the one
    ``_run_timing_check`` would turn into its gate reason.
    """
    from modules import timing_check as tc
    from modules.verification_runner import HungTest

    def runner(worktree: str, test_ids: list[str]) -> str:
        env = tc.drift_env()
        files = {test.split("::", 1)[0] for test in test_ids if (Path(worktree) / test.split("::", 1)[0]).is_file()}
        collected = tc._collected_ids(Path(worktree), files, env, python)
        if collected is None:
            return "error: could not collect the test ids on main"
        on_main = [test for test in test_ids if test in collected]
        if not on_main:
            return "error: none of the test ids exist on main"
        options: dict[str, Any] = {}
        if watchdog is not None:
            options = {"watchdog": watchdog, "run_id": "gate-self-repair-timing"}
        try:
            result = tc.run_timing_check(worktree, on_main, python=python, env=env, **options)
            flagged = tc.flagged_tests(result)
            if not flagged:
                return result  # "ok" or an error without a verdict
            if watchdog is not None:
                options["run_id"] = "gate-self-repair-timing-control"
            control = tc.control_check(worktree, flagged, python=python, **options)
        except HungTest as exc:
            return f"error: watchdog stopped the rerun ({exc})"
        if control is None:
            return "error: control run without drift gave no verdict"
        drift_only, fails_without_drift = control
        if drift_only:
            return f"{tc.TIMING_SENSITIVE_PREFIX}{', '.join(drift_only)}"
        return FAILS_WITHOUT_DRIFT_RESULT + ", ".join(fails_without_drift)

    return runner


def classify_rerun(result: str | None, reason: str) -> str:
    """The verdict of a rerun on main for the detected normalized *reason*.

    Only the same normalized gate reason counts as failing on main too;
    ``ok`` passes, ``timing_sensitive: ...`` means the tests really are
    timing-sensitive (not the host), and anything else gives no verdict.
    """
    text = str(result or "")
    if text == "ok":
        return VERDICT_PASSED
    if normalize_gate_reason(f"{GATE_REASON_PREFIX} {text}") == reason and extract_test_ids(text):
        return VERDICT_FAILED
    if text.startswith("timing_sensitive: "):
        return VERDICT_TIMING_SENSITIVE
    return VERDICT_NO_VERDICT


def rerun_gate_on_main(
    repo_root: str,
    test_ids: list[str],
    *,
    reason: str,
    runner: Runner | None = None,
    python: str | None = None,
    watchdog: Any | None = None,
) -> dict[str, Any]:
    """Rerun the timing gate once on current ``main`` and classify the outcome.

    Never raises for a failing rerun: any exception (no main, worktree
    trouble, a crashed runner) is ``no_verdict``, which switches nothing off.
    """
    main_sha: str | None = None
    try:
        from modules.experiment_service.reverify import current_main

        main_sha = current_main(str(repo_root))
        if not test_ids:
            result = "error: no test ids recorded in the gate reasons"
        else:
            if runner is None:
                runner = default_gate_runner(python=python, watchdog=watchdog)
            with _main_worktree(str(repo_root), main_sha) as worktree:
                result = runner(str(worktree), list(test_ids))
    except Exception as exc:  # noqa: BLE001 - a crashed rerun is no verdict
        result = f"error: rerun on main crashed: {type(exc).__name__}: {str(exc)[:400]}"
    verdict = classify_rerun(result, reason)
    return {
        "main_sha": main_sha,
        "result": result,
        "verdict": verdict,
        "passed": verdict == VERDICT_PASSED,
        "test_ids": list(test_ids),
    }


# --------------------------------------------------------------------------- #
# Persisted rerun records (one rerun per experiment triple)
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


def rerun_record(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    record = _read_json_setting(conn, key)
    return record if isinstance(record, dict) else None


def claim_rerun(
    conn: sqlite3.Connection, detection: dict[str, Any], now: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Claim the one rerun of this triple: ``(record, claimed_now)``.

    The claim is written (``running``) before the rerun starts, so a crash
    or a parallel tick never leads to a second rerun of the same triple.
    """
    now = now or _utcnow()
    key = detection["key"]
    record = {
        "key": key,
        "status": RERUN_RUNNING,
        "reason": detection["reason"],
        "experiments": list(detection["experiments"]),
        "issues": list(detection["issues"]),
        "test_ids": list(detection["test_ids"]),
        "started_at": now,
        "finished_at": None,
        "main_sha": None,
        "result": None,
        "verdict": None,
        "applied": False,
    }
    with _transaction(conn):
        cursor = conn.execute(
            "INSERT INTO service_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO NOTHING",
            (key, json.dumps(record), now),
        )
        claimed = cursor.rowcount == 1
        if claimed:
            conn.execute(_SETTINGS_UPSERT, (SETTING_LAST_RERUN, json.dumps(key), now))
    if claimed:
        return record, True
    return rerun_record(conn, key) or record, False


def finish_rerun(
    conn: sqlite3.Connection, record: dict[str, Any], rerun: dict[str, Any], now: str | None = None,
) -> dict[str, Any]:
    """Store the rerun's outcome with its claim."""
    now = now or _utcnow()
    record = {
        **record,
        "status": RERUN_DONE,
        "finished_at": now,
        "main_sha": rerun.get("main_sha"),
        "result": rerun.get("result"),
        "verdict": rerun.get("verdict"),
    }
    with _transaction(conn):
        conn.execute(_SETTINGS_UPSERT, (record["key"], json.dumps(record), now))
    return record


# --------------------------------------------------------------------------- #
# Persisted switch-off (settings DB)
# --------------------------------------------------------------------------- #

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
    if not db_path or db_path == ":memory:" or not os.path.exists(db_path):
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


def _check_switch_off_allowed(conn: sqlite3.Connection, gate: str) -> list[dict[str, Any]]:
    if gate not in ALLOWED_SWITCH_OFF_GATES:
        raise ValueError(
            f"gate {gate!r} is not allowlisted for switch-off "
            f"(allowlist: {sorted(ALLOWED_SWITCH_OFF_GATES)}; protected: {list(PROTECTED_GATES)})"
        )
    current = disabled_gates(conn)
    if current and all(entry.get("gate") != gate for entry in current):
        raise ValueError(
            "at most one gate can be switched off at a time; already off: "
            + ", ".join(str(entry.get("gate")) for entry in current)
        )
    return current


def _report_key(entry: dict[str, Any], kind: str) -> str:
    return f"{entry.get('switch_off_id')}:{kind}"


def _insert_missing_reports(conn: sqlite3.Connection, entry: dict[str, Any], now: str) -> int:
    """Insert the GitHub and mailbox report rows of *entry* that do not exist yet.

    Keyed by ``report_key``, so calling it twice never duplicates a report.
    Does not commit. Returns how many rows were inserted.
    """
    body = _report_body(entry)
    inserted = 0
    for kind in REPORT_KINDS:
        key = _report_key(entry, kind)
        payload = {
            "title": REPAIR_ISSUE_TITLE,
            "body": body,
            "switch_off_id": entry.get("switch_off_id"),
            "report_key": key,
            "created_at": now,
        }
        cursor = conn.execute(
            f"INSERT INTO {REPORT_TABLE} "
            "(kind, payload_json, status, attempts, report_key, created_at, updated_at) "
            "VALUES (?, ?, 'pending', 0, ?, ?, ?) ON CONFLICT(report_key) DO NOTHING",
            (kind, json.dumps(payload), key, now, now),
        )
        inserted += cursor.rowcount
    return inserted


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
    rerun_key: str | None = None,
    repair_issue_number: int | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Persist the switch-off of *gate* and its pending reports in one transaction.

    Only allowlisted gates may be switched off (``timing_check`` only), and at
    most one gate may be off at a time; anything else raises ``ValueError``
    and changes nothing. The disabled-gate list, the state, the rerun
    record's ``applied`` flag and both pending report rows are committed
    together, so a crash can never leave the gate off without its report.
    """
    now = now or _utcnow()
    with _transaction(conn):
        current = _check_switch_off_allowed(conn, gate)
        entry = {
            "gate": gate,
            "switch_off_id": uuid.uuid4().hex,
            "classification": classification,
            "reason": reason,
            "test_ids": list(test_ids),
            "experiments": list(experiments),
            "issues": list(issues),
            "main_sha": main_sha,
            "rerun_result": rerun_result,
            "rerun_key": rerun_key,
            "repair_issue_number": repair_issue_number,
            "disabled_at": now,
            "updated_at": now,
        }
        entries = [e for e in current if e.get("gate") != gate] + [entry]
        conn.execute(_SETTINGS_UPSERT, (SETTING_DISABLED_GATES, json.dumps(entries), now))
        conn.execute(_SETTINGS_UPSERT, (SETTING_STATE, json.dumps(entry), now))
        if rerun_key:
            record = rerun_record(conn, rerun_key)
            if record is not None:
                record["applied"] = True
                record["switch_off_id"] = entry["switch_off_id"]
                conn.execute(_SETTINGS_UPSERT, (rerun_key, json.dumps(record), now))
        _insert_missing_reports(conn, entry, now)
    return entry


def _store_repair_issue_number(
    conn: sqlite3.Connection, gate: str, switch_off_id: str | None, issue_number: int, now: str,
) -> dict[str, Any] | None:
    """Store the repair issue's number with the matching switch-off. No commit."""
    entry = gate_disabled(conn, gate)
    if entry is None:
        return None
    if switch_off_id is not None and entry.get("switch_off_id") not in (None, switch_off_id):
        return None  # a report of an earlier switch-off; do not mislabel this one
    entry["repair_issue_number"] = int(issue_number)
    entry["updated_at"] = now
    entries = [e for e in disabled_gates(conn) if e.get("gate") != gate] + [entry]
    conn.execute(_SETTINGS_UPSERT, (SETTING_DISABLED_GATES, json.dumps(entries), now))
    conn.execute(_SETTINGS_UPSERT, (SETTING_STATE, json.dumps(entry), now))
    return entry


def set_repair_issue_number(
    conn: sqlite3.Connection, gate: str, issue_number: int, now: str | None = None,
) -> dict[str, Any] | None:
    """Store the opened repair issue's number with the switch-off."""
    now = now or _utcnow()
    with _transaction(conn):
        return _store_repair_issue_number(conn, gate, None, issue_number, now)


def ensure_reports_for_switch_off(conn: sqlite3.Connection, now: str | None = None) -> int:
    """Re-enqueue the report rows of a switch-off that has none (self-repair).

    Covers a switch-off written without its reports, for instance by hand or
    by an older version. Returns how many rows were inserted.
    """
    now = now or _utcnow()
    with _transaction(conn):
        entry = gate_disabled(conn, GATE_TIMING_CHECK)
        if entry is None:
            return 0
        if not entry.get("switch_off_id"):
            entry["switch_off_id"] = uuid.uuid4().hex
            entries = [e for e in disabled_gates(conn) if e.get("gate") != GATE_TIMING_CHECK] + [entry]
            conn.execute(_SETTINGS_UPSERT, (SETTING_DISABLED_GATES, json.dumps(entries), now))
            conn.execute(_SETTINGS_UPSERT, (SETTING_STATE, json.dumps(entry), now))
        return _insert_missing_reports(conn, entry, now)


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
    last_key = _read_json_setting(conn, SETTING_LAST_RERUN)
    last_rerun = rerun_record(conn, last_key) if isinstance(last_key, str) else None
    return {
        "disabled_gates": [entry.get("gate") for entry in entries],
        "timing_check": "off" if timing_entry is not None else "on",
        "host_blocker": (latest or {}).get("classification"),
        "repair_issue_number": (latest or {}).get("repair_issue_number"),
        "state": latest,
        "pending_reports": pending,
        "last_rerun": last_rerun,
    }


def _fallback_state() -> dict[str, Any]:
    return {
        "disabled_gates": [], "timing_check": "on", "host_blocker": None,
        "repair_issue_number": None, "state": None, "pending_reports": 0,
        "last_rerun": None,
    }


def status(db_path: str | None = None) -> dict[str, Any]:
    """The gate self-repair state read from *db_path* (default: the configured DB)."""
    path = db_path or _default_db_path()
    if path == ":memory:" or not os.path.exists(path):
        return _fallback_state()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return _fallback_state()
    try:
        return gate_state(conn)
    except sqlite3.Error:
        return _fallback_state()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Durable pending reports (GitHub / mailbox), retried on the next tick
# --------------------------------------------------------------------------- #

def enqueue_report(conn: sqlite3.Connection, kind: str, payload: dict[str, Any], now: str | None = None) -> int:
    """Insert a durable pending report row; returns its id."""
    now = now or _utcnow()
    with _transaction(conn):
        cursor = conn.execute(
            f"INSERT INTO {REPORT_TABLE} (kind, payload_json, status, attempts, report_key, created_at, updated_at) "
            "VALUES (?, ?, 'pending', 0, ?, ?, ?)",
            (kind, json.dumps(payload), payload.get("report_key"), now, now),
        )
    return int(cursor.lastrowid)


def pending_reports(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        return _rows(conn, f"SELECT * FROM {REPORT_TABLE} WHERE status='pending' ORDER BY id", ())
    except sqlite3.OperationalError:
        return []


def retry_pending_reports(conn: sqlite3.Connection, deliver: Deliver, *, now: str | None = None) -> list[dict[str, Any]]:
    """Deliver every pending report row once; failures stay pending for the next tick.

    Marking a row done and storing the repair issue's number with the
    switch-off happen in one transaction. Should the process die after a
    delivery but before that commit, the row stays pending and the next
    delivery is idempotent (see :func:`default_deliver`).
    """
    now = now or _utcnow()
    results: list[dict[str, Any]] = []
    for row in pending_reports(conn):
        try:
            payload = json.loads(row["payload_json"])
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
            result = deliver(row["kind"], payload)
        except Exception as exc:  # noqa: BLE001 - a failed delivery is retried, never lost
            with _transaction(conn):
                conn.execute(
                    f"UPDATE {REPORT_TABLE} SET attempts=attempts+1, last_error=?, updated_at=? WHERE id=?",
                    (f"{type(exc).__name__}: {str(exc)[:500]}", now, row["id"]),
                )
            results.append({
                "id": row["id"], "kind": row["kind"], "status": "pending",
                "attempts": row["attempts"] + 1, "error": str(exc)[:500],
            })
            continue
        with _transaction(conn):
            conn.execute(
                f"UPDATE {REPORT_TABLE} SET status='done', attempts=attempts+1, result_json=?, "
                "last_error=NULL, updated_at=? WHERE id=? AND status='pending'",
                (json.dumps(result), now, row["id"]),
            )
            if row["kind"] == REPORT_KIND_GITHUB_ISSUE and isinstance(result, dict):
                number = result.get("number")
                if number is not None:
                    _store_repair_issue_number(
                        conn, GATE_TIMING_CHECK, payload.get("switch_off_id"), number, now,
                    )
        results.append({"id": row["id"], "kind": row["kind"], "status": "done", "result": result})
    return results


def _gh_create_issue(repo_root: str, title: str, body: str) -> dict[str, Any]:
    """Open a plain issue via ``gh`` - deliberately *no* ``--label``, so the
    repair issue never carries the ``proposal`` label (and never goes through
    ``open_need_issue`` with its proposal footer).

    Idempotent: an open issue with exactly this title is reused, so a retry
    after a crash between creating and recording never opens a second one.
    """
    listed = subprocess.run(
        ["gh", "issue", "list", "--state", "open", "--search", f'"{title}" in:title',
         "--json", "number,title,url", "--limit", "20"],
        cwd=repo_root, capture_output=True, text=True, timeout=60, check=False,
    )
    if listed.returncode != 0:
        raise RuntimeError(listed.stderr.strip()[:400] or f"gh issue list exited {listed.returncode}")
    try:
        existing = json.loads(listed.stdout or "[]")
    except ValueError as exc:
        raise RuntimeError(f"unreadable gh issue list output: {exc}") from exc
    for issue in existing if isinstance(existing, list) else []:
        if isinstance(issue, dict) and issue.get("title") == title and issue.get("number"):
            return {"number": int(issue["number"]), "url": issue.get("url"), "reused": True}
    proc = subprocess.run(
        ["gh", "issue", "create", "--title", title, "--body", body],
        cwd=repo_root, capture_output=True, text=True, timeout=60, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:400] or f"gh exited {proc.returncode}")
    match = re.search(r"/issues/(\d+)", proc.stdout or "")
    return {"number": int(match.group(1)) if match else None, "url": (proc.stdout or "").strip()}


def mailbox_file_name(payload: dict[str, Any]) -> str:
    """The fixed outbox file name of a report (same payload -> same name)."""
    stamp = re.sub(r"[^0-9]", "", str(payload.get("created_at") or ""))[:14] or "00000000000000"
    identity = str(payload.get("report_key") or json.dumps(payload, sort_keys=True))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{stamp}-gate-repair-{digest}.md"


def _mailbox_post(payload: dict[str, Any], outbox_dir: Path | None = None) -> dict[str, Any]:
    """Write the report into the mailbox repository's outbox (idempotent)."""
    if outbox_dir is None:
        from modules.mailbox.config import MailboxConfig

        outbox_dir = MailboxConfig.from_env().outbox_dir  # raises when unset
    outbox_dir.mkdir(parents=True, exist_ok=True)
    path = outbox_dir / mailbox_file_name(payload)
    if path.exists():
        return {"outbox_file": path.name, "reused": True}
    content = "\n".join([
        "---",
        "type: report",
        "status: sent",
        f"processed_at: {payload.get('created_at') or _utcnow()}",
        "created_by: nexus",
        "---",
        "",
        f"# {payload.get('title') or REPAIR_ISSUE_TITLE}",
        "",
        str(payload.get("body") or ""),
    ]) + "\n"
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


def _report_body(entry: dict[str, Any]) -> str:
    issues = ", ".join(f"#{issue}" for issue in entry.get("issues") or []) or "unknown"
    tests = ", ".join(f"`{test}`" for test in entry.get("test_ids") or []) or "none recorded"
    experiments = ", ".join(str(e) for e in entry.get("experiments") or []) or "unknown"
    return "\n".join([
        "Nexus detected a repeated host-level blocker and switched one gate off (#730).",
        "",
        f"- Classification: `{entry.get('classification')}`",
        f"- Gate: `{entry.get('gate')}` (allowlisted; review, the test suite, the main guard and the safeguards are never switched off, and at most one gate is off at a time)",
        f"- Normalized gate reason: `{entry.get('reason')}`",
        f"- Last finished experiments: {experiments} on issues {issues}",
        f"- Affected test ids: {tests}",
        f"- Gate rerun once on current main (`{entry.get('main_sha')}`) with the same test ids in the verification environment (drift run, then control run without drift): `{entry.get('rerun_result')}`",
        "",
        "The timing check is now off persistently (settings DB, key "
        f"`{SETTING_DISABLED_GATES}`); `timing_check_enabled()` honours it across restarts.",
        "To re-enable it, fix the host cause and set that key back to `[]` in `service_settings`.",
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
    watchdog: Any | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Detect a repeated host blocker and switch the allowlisted gate off.

    Called once per tick. Pending reports from earlier ticks are retried first,
    so a failed GitHub/mailbox delivery is never lost. The rerun on main runs
    at most once per detected experiment triple. Returns the resulting
    :func:`gate_state` plus what happened (``switched_off``, ``detected``,
    ``rerun``, ``rerun_skipped``, ``reports``).
    """
    now = now or _utcnow()
    path = db_path or _default_db_path()
    if path != ":memory:" and not os.path.exists(path):
        return {"switched_off": False, "detected": None, **status(path)}
    conn = sqlite3.connect(path, timeout=30)
    try:
        ensure_schema(conn)
        deliver = deliver if deliver is not None else default_deliver(repo_root)
        if gate_disabled(conn, GATE_TIMING_CHECK) is not None:
            # A switch-off whose reports went missing gets them back first.
            ensure_reports_for_switch_off(conn, now=now)
            retried = retry_pending_reports(conn, deliver, now=now)
            return {**gate_state(conn), "switched_off": False, "already_off": True, "retried_reports": retried}
        retried = retry_pending_reports(conn, deliver, now=now)
        base = {"switched_off": False, "retried_reports": retried}
        if disabled_gates(conn):
            # Another gate is listed off (never by this module): at most one.
            return {**gate_state(conn), **base, "detected": None, "blocked_by_other_gate": True}
        detection = detect_repeated_blocker(conn)
        if detection is None:
            return {**gate_state(conn), **base, "detected": None}
        record, claimed = claim_rerun(conn, detection, now=now)
        rerun: dict[str, Any] | None = None
        if claimed:
            rerun = rerun_gate_on_main(
                repo_root, detection["test_ids"], reason=detection["reason"],
                runner=runner, python=python, watchdog=watchdog,
            )
            record = finish_rerun(conn, record, rerun, now=now)
        elif record.get("status") != RERUN_DONE:
            # Claimed by a rerun that is still running elsewhere or was
            # interrupted: no verdict, and never a second rerun.
            return {**gate_state(conn), **base, "detected": detection, "rerun_skipped": "in_progress_or_interrupted", "rerun_record": record}
        if record.get("verdict") != VERDICT_FAILED or record.get("applied"):
            outcome = {**gate_state(conn), **base, "detected": detection, "rerun_record": record}
            if rerun is not None:
                outcome["rerun"] = rerun
            else:
                outcome["rerun_skipped"] = "already_checked"
            return outcome
        # The rerun failed on main too. Also reached on a later tick when the
        # switch-off itself was interrupted after the rerun was stored.
        classification = f"{HOST_BLOCKER_PREFIX}{GATE_TIMING_CHECK}"
        entry = disable_gate(
            conn, GATE_TIMING_CHECK,
            classification=classification,
            reason=detection["reason"],
            test_ids=detection["test_ids"],
            experiments=detection["experiments"],
            issues=detection["issues"],
            main_sha=record.get("main_sha"),
            rerun_result=record.get("result"),
            rerun_key=detection["key"],
            now=now,
        )
        delivered = retry_pending_reports(conn, deliver, now=now)
        outcome = {
            **gate_state(conn), "switched_off": True, "blocker": classification,
            "detected": detection, "rerun_record": record, "reports": delivered,
            "entry": entry, "retried_reports": retried,
        }
        if rerun is not None:
            outcome["rerun"] = rerun
        else:
            outcome["rerun_skipped"] = "already_checked"
        return outcome
    finally:
        conn.close()
