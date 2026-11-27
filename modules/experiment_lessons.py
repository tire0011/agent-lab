"""Lessons store: what a failed-then-fixed experiment pair teaches the next run (#715).

Nexus repeats mistakes it has already solved once (wall-clock-dependent tests
were reverted in #675 and fixed in #681/#689; the ``AGENT_API_KEY`` ``setdefault``
problem blocked four experiments in #713). That knowledge exists only as
experiment rows, so it never reaches the next coding run. This module closes
the loop:

1. Record: when a terminal experiment on some source (usually a GitHub issue)
   has both a failure (review finding, test failure or gate) and a later
   success, exactly one lesson is stored per failed-then-succeeded pair:
   ``cause`` -> ``rule`` with the evidence experiment ids, the touched files
   and keywords. The rule is phrased with the local model when one is
   available (:func:`phrase_pending_lessons`); the review/test/gate finding
   text is the immediate fallback.
2. Use: every new coding prompt gets the 3-5 best-matching lessons as a short
   "Lessons from earlier runs" section (:func:`lessons_for_prompt`), bounded
   to ``LESSONS_SECTION_MAX_CHARS`` so the prompt budget holds.
3. Clean up: a lesson whose injected failure reason happened again is counted
   ineffective and demoted; a lesson unused for ``STALE_UNUSED_DAYS`` is
   demoted; the store is hard-capped at ``MAX_LESSONS_TOTAL`` non-removed rows.
4. Visible: :func:`list_lessons` backs the ``lessons_list`` operation and the
   ``GET /lessons`` endpoint.

The status strings mirror ``modules.experiment_service.models``; they are
kept local so this leaf module never imports the experiment service (import
cycle), and only ever compared as strings.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from modules.run_store import DEFAULT_DB_PATH, init_db

logger = logging.getLogger(__name__)

LESSONS_SECTION_TITLE = "Lessons from earlier runs"
# #715: 3-5 lessons per coding prompt; the selection limit is clamped to this.
MAX_LESSONS_PER_PROMPT = 5
# Hard cap on stored (non-removed) lessons, so the store stays small.
MAX_LESSONS_TOTAL = 50
# Prompt-budget bound for the rendered section appended to a coding prompt.
LESSONS_SECTION_MAX_CHARS = 1600
MAX_RULE_CHARS = 300
MAX_CAUSE_CHARS = 500
MAX_KEYWORDS = 24
# A lesson that was injected but whose failure reason happened anyway this
# many times is demoted.
INEFFECTIVE_DEMOTE_THRESHOLD = 2
# An active lesson not injected for this long is demoted as stale.
STALE_UNUSED_DAYS = 30

STATUS_ACTIVE = "active"
STATUS_DEMOTED = "demoted"
STATUS_REMOVED = "removed"

# Terminal statuses of modules.experiment_service.models, duplicated here on
# purpose (see module docstring). Only these drive lesson recording.
STATUS_SUCCEEDED = "succeeded"
FAILURE_STATUSES = ("failed", "timed_out", "needs_review")
_TERMINAL_STATUSES = (
    "rejected", STATUS_SUCCEEDED, "failed", "timed_out", "needs_review",
    "cancelled", "interrupted",
)

_WORD_RE = re.compile(r"[a-z0-9_./-]{3,}")
_BUSY_TIMEOUT_MS = 10_000


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the lessons tables (idempotent); called from run_store.init_db."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS experiment_lessons (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            source_type TEXT NULL,
            source_id TEXT NULL,
            failure_experiment_id TEXT NOT NULL,
            success_experiment_id TEXT NOT NULL,
            failure_kind TEXT NULL,
            cause TEXT NOT NULL,
            rule TEXT NOT NULL,
            -- 1 once the local model phrased the rule, 0 while the finding
            -- text is the fallback rule.
            phrased INTEGER NOT NULL DEFAULT 0 CHECK (phrased IN (0, 1)),
            model TEXT NULL,
            files_json TEXT NULL,
            keywords_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'demoted', 'removed')),
            injected_count INTEGER NOT NULL DEFAULT 0,
            ineffective_count INTEGER NOT NULL DEFAULT 0,
            last_injected_at TEXT NULL,
            demoted_at TEXT NULL,
            -- one lesson per failed-then-succeeded pair
            UNIQUE (failure_experiment_id, success_experiment_id)
        );

        CREATE INDEX IF NOT EXISTS idx_experiment_lessons_status
            ON experiment_lessons(status, created_at DESC);

        -- which coding run was shown which lesson (injection tracking)
        CREATE TABLE IF NOT EXISTS experiment_lesson_injections (
            lesson_id TEXT NOT NULL,
            experiment_id TEXT NOT NULL,
            injected_at TEXT NOT NULL,
            PRIMARY KEY (lesson_id, experiment_id)
        );

        CREATE INDEX IF NOT EXISTS idx_lesson_injections_experiment
            ON experiment_lesson_injections(experiment_id);
        """
    )


# ---- small helpers --------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _parse_ts(value: Any) -> datetime:
    """Parse a stored timestamp; unreadable values sort as the epoch so a
    malformed row never wins a "later than" comparison."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _changed_files(row: dict[str, Any]) -> list[str]:
    """File names from a porcelain ``git_status`` (plus ``diff_stat`` fallback)."""
    names: list[str] = []
    for line in (row.get("git_status") or "").splitlines():
        text = line.strip()
        if not text:
            continue
        name = text[2:].strip() if len(text) > 2 else text
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        if name and name not in names:
            names.append(name)
    if not names:
        stat = str(row.get("diff_stat") or "").strip()
        for line in stat.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[2] not in names:
                names.append(parts[2])
    return names


def _keywords(text: str, files: Iterable[str] = ()) -> list[str]:
    """Significant lowercased tokens of *text* plus file basenames."""
    seen: set[str] = set()
    words: list[str] = []
    for token in _WORD_RE.findall((text or "").lower()):
        token = token.strip("./-")
        if len(token) >= 4 and not token.isdigit() and token not in seen:
            seen.add(token)
            words.append(token)
    for path in files or ():
        base = str(path).rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0].lower()
        if len(stem) >= 3 and stem not in seen:
            seen.add(stem)
            words.append(stem)
    return words[:MAX_KEYWORDS]


def _failure_kind(row: dict[str, Any]) -> str:
    """Coarse classification of why the failed experiment ended."""
    if row.get("status") == "timed_out":
        return "timeout"
    if row.get("test_exit_code") not in (None, 0):
        return "test"
    quality = row.get("quality_evaluation_json")
    if quality:
        try:
            outcome = (json.loads(quality) or {}).get("outcome")
        except (TypeError, json.JSONDecodeError):
            outcome = None
        if outcome is not None and outcome != "pass":
            return "gate"
    if row.get("status") == "needs_review":
        return "review"
    return "other"


def _cause_text(row: dict[str, Any]) -> str:
    """The short finding text of the failed experiment (error, then gate)."""
    parts: list[str] = []
    error = str(row.get("error") or "").strip()
    if error:
        parts.append(error)
    quality = row.get("quality_evaluation_json")
    if quality and not error:
        try:
            parsed = json.loads(quality) or {}
            reason = parsed.get("reason") or parsed.get("review_findings")
            if reason:
                parts.append(str(reason))
        except (TypeError, json.JSONDecodeError):
            pass
    if not parts:
        parts.append(f"experiment ended as {row.get('status') or 'unknown'}")
    text = "; ".join(parts)
    return text if len(text) <= MAX_CAUSE_CHARS else text[: MAX_CAUSE_CHARS - 3] + "..."


def _clean_rule(raw: Any) -> str:
    """One short plain line out of a model reply; '' when nothing usable."""
    text = str(raw or "").strip()
    if not text:
        return ""
    line = text.splitlines()[0].strip()
    line = line.lstrip("-#* ").strip().strip("`").strip('"').strip("'").strip()
    if not line:
        return ""
    return line if len(line) <= MAX_RULE_CHARS else line[: MAX_RULE_CHARS - 3] + "..."


def _phrasing_prompt(cause: str, files: list[str], failure_kind: str) -> str:
    return (
        "A coding experiment failed and a later attempt on the same issue fixed it. "
        "Turn this into one short rule for future coding runs.\n"
        f"Failure ({failure_kind}): {cause}\n"
        f"Files involved: {', '.join(files[:10]) or 'unknown'}\n"
        "Reply with exactly one line of at most 30 words, starting with a verb, "
        "no markdown, no quotes. Example: \"Inject the clock; never compute token "
        "budgets from time.monotonic() deltas in tests.\""
    )


def _row_to_lesson(row: sqlite3.Row | tuple) -> dict[str, Any]:
    data = dict(row) if not isinstance(row, dict) else row
    try:
        files = json.loads(data.get("files_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        files = []
    try:
        keywords = json.loads(data.get("keywords_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        keywords = []
    return {
        "id": data["id"],
        "created_at": data["created_at"],
        "source_type": data.get("source_type"),
        "source_id": data.get("source_id"),
        "failure_experiment_id": data["failure_experiment_id"],
        "success_experiment_id": data["success_experiment_id"],
        "failure_kind": data.get("failure_kind"),
        "cause": data["cause"],
        "rule": data["rule"],
        "phrased": bool(data.get("phrased")),
        "model": data.get("model"),
        "files": [str(f) for f in files if isinstance(f, str)],
        "keywords": [str(k) for k in keywords if isinstance(k, str)],
        "status": data.get("status") or STATUS_ACTIVE,
        "injected_count": int(data.get("injected_count") or 0),
        "ineffective_count": int(data.get("ineffective_count") or 0),
        "last_injected_at": data.get("last_injected_at"),
        "demoted_at": data.get("demoted_at"),
    }


def _lesson_files(lesson: dict[str, Any]) -> list[str]:
    return list(lesson.get("files") or [])


def _file_match(candidate: str, lesson_file: str) -> bool:
    """Path-insensitive-ish match: equal, same basename, or suffix of either."""
    a = str(candidate).strip().lstrip("./")
    b = str(lesson_file).strip().lstrip("./")
    if not a or not b:
        return False
    return a == b or a.rsplit("/", 1)[-1] == b.rsplit("/", 1)[-1] or a.endswith("/" + b) or b.endswith("/" + a)


# ---- record ---------------------------------------------------------------


def enforce_hard_cap(
    conn: sqlite3.Connection, *, now: str | None = None, cap: int = MAX_LESSONS_TOTAL
) -> int:
    """Keep at most *cap* non-removed lessons; evict the least valuable ones.

    Least valuable = never/rarely injected, then oldest use. Returns how many
    rows were removed.
    """
    ensure_schema(conn)
    cap = max(1, int(cap))
    stamp = now or _utcnow_iso()
    with conn:
        rows = conn.execute(
            """SELECT id FROM experiment_lessons WHERE status != ?
               ORDER BY injected_count ASC, COALESCE(last_injected_at, created_at) ASC,
                        created_at ASC""",
            (STATUS_REMOVED,),
        ).fetchall()
        excess = len(rows) - cap
        removed = 0
        if excess > 0:
            for row in rows[:excess]:
                conn.execute(
                    """UPDATE experiment_lessons SET status=?, demoted_at=COALESCE(demoted_at, ?)
                       WHERE id=? AND status != ?""",
                    (STATUS_REMOVED, stamp, row[0], STATUS_REMOVED),
                )
                removed += 1
    return removed


def record_lesson(
    conn: sqlite3.Connection,
    failure_row: dict[str, Any],
    success_row: dict[str, Any],
    *,
    llm: Callable[[str], Any] | None = None,
    now: str | None = None,
) -> dict[str, Any] | None:
    """Store exactly one lesson for a failed-then-succeeded pair.

    Idempotent: an existing lesson for the pair is returned unchanged. *llm*,
    when given, phrases the rule (``llm(prompt) -> text``); any model error
    keeps the finding text as the fallback rule.
    """
    ensure_schema(conn)
    failure_id = str(failure_row["id"])
    success_id = str(success_row["id"])
    existing = conn.execute(
        """SELECT * FROM experiment_lessons
           WHERE failure_experiment_id=? AND success_experiment_id=?""",
        (failure_id, success_id),
    ).fetchone()
    if existing is not None:
        return _row_to_lesson(existing)

    files = sorted(set(_changed_files(failure_row)) | set(_changed_files(success_row)))
    kind = _failure_kind(failure_row)
    cause = _cause_text(failure_row)
    keywords = _keywords(
        f"{failure_row.get('goal') or ''} {cause} {success_row.get('goal') or ''}", files
    )
    rule = cause[:MAX_RULE_CHARS]
    phrased = 0
    model: str | None = None
    if llm is not None:
        try:
            candidate = _clean_rule(llm(_phrasing_prompt(cause, files, kind)))
            if candidate:
                rule = candidate
                phrased = 1
                model = str(getattr(llm, "model", "local"))
        except Exception as exc:  # noqa: BLE001 - the finding text is the fallback
            logger.warning("experiment_lessons: rule phrasing failed: %s", exc)

    lesson_id = uuid.uuid4().hex
    stamp = now or _utcnow_iso()
    with conn:
        conn.execute(
            """INSERT INTO experiment_lessons
               (id, created_at, source_type, source_id, failure_experiment_id,
                success_experiment_id, failure_kind, cause, rule, phrased, model,
                files_json, keywords_json, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
               ON CONFLICT (failure_experiment_id, success_experiment_id) DO NOTHING""",
            (
                lesson_id, stamp, failure_row.get("source_type"), failure_row.get("source_id"),
                failure_id, success_id, kind, cause, rule, phrased, model,
                json.dumps(files, ensure_ascii=False), json.dumps(keywords, ensure_ascii=False),
            ),
        )
        # Enforced after the insert so the store never exceeds the cap, even
        # when this very row is the one that crosses it.
        enforce_hard_cap(conn, now=stamp)
    row = conn.execute(
        "SELECT * FROM experiment_lessons WHERE failure_experiment_id=? AND success_experiment_id=?",
        (failure_id, success_id),
    ).fetchone()
    return _row_to_lesson(row) if row is not None else None


def _reason_matches(lesson: dict[str, Any], error_text: str) -> bool:
    """Whether *error_text* repeats the failure reason the lesson warns about.

    Counts shared significant tokens (keywords plus long cause words); three
    or more mean the same failure happened again despite the injected lesson.
    """
    hay = (error_text or "").lower()
    if not hay.strip():
        return False
    tokens = {k for k in lesson.get("keywords", []) if len(k) >= 5}
    tokens.update(
        w for w in _WORD_RE.findall(str(lesson.get("cause") or "").lower())
        if len(w) >= 6
    )
    matched = sum(1 for token in tokens if token in hay)
    return matched >= 3


def mark_ineffective(conn: sqlite3.Connection, lesson_id: str, *, now: str | None = None) -> str:
    """Count one repeated failure for *lesson_id*; demote at the threshold.

    Returns the lesson's status after the update.
    """
    ensure_schema(conn)
    stamp = now or _utcnow_iso()
    with conn:
        conn.execute(
            "UPDATE experiment_lessons SET ineffective_count = ineffective_count + 1 WHERE id=?",
            (lesson_id,),
        )
        row = conn.execute(
            "SELECT ineffective_count, status FROM experiment_lessons WHERE id=?", (lesson_id,)
        ).fetchone()
        if row is not None and row[0] >= INEFFECTIVE_DEMOTE_THRESHOLD and row[1] == STATUS_ACTIVE:
            conn.execute(
                """UPDATE experiment_lessons SET status=?, demoted_at=?
                   WHERE id=? AND status=?""",
                (STATUS_DEMOTED, stamp, lesson_id, STATUS_ACTIVE),
            )
            return STATUS_DEMOTED
    return str(row[1]) if row is not None else STATUS_ACTIVE


def _mark_repeated_failures(
    conn: sqlite3.Connection, failed_row: dict[str, Any], *, now: str | None = None
) -> list[str]:
    """Demote lessons that were injected into *failed_row* but did not help."""
    error_text = f"{failed_row.get('error') or ''}\n{failed_row.get('test_output') or ''}"
    injected = conn.execute(
        "SELECT lesson_id FROM experiment_lesson_injections WHERE experiment_id=?",
        (failed_row["id"],),
    ).fetchall()
    demoted: list[str] = []
    for (lesson_id,) in injected:
        row = conn.execute(
            "SELECT * FROM experiment_lessons WHERE id=?", (lesson_id,)
        ).fetchone()
        if row is None or row["status"] != STATUS_ACTIVE:
            continue
        lesson = _row_to_lesson(row)
        if _reason_matches(lesson, error_text):
            if mark_ineffective(conn, lesson_id, now=now) == STATUS_DEMOTED:
                demoted.append(lesson_id)
    return demoted


def demote_stale_lessons(
    conn: sqlite3.Connection, *, now: str | None = None, stale_days: int = STALE_UNUSED_DAYS
) -> int:
    """Demote active lessons not injected for *stale_days*; returns the count."""
    ensure_schema(conn)
    stamp = now or _utcnow_iso()
    cutoff = (_parse_ts(stamp) - timedelta(days=max(1, int(stale_days)))).isoformat()
    with conn:
        cursor = conn.execute(
            """UPDATE experiment_lessons SET status=?, demoted_at=COALESCE(demoted_at, ?)
               WHERE status=?
                 AND (last_injected_at IS NULL AND created_at < ?
                      OR last_injected_at IS NOT NULL AND last_injected_at < ?)""",
            (STATUS_DEMOTED, stamp, STATUS_ACTIVE, cutoff, cutoff),
        )
    return cursor.rowcount


def note_experiment_finished(
    db_path: str,
    experiment_id: str,
    *,
    llm: Callable[[str], Any] | None = None,
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Bookkeeping after an experiment reached a terminal status (#715).

    Creates one lesson per failed-then-succeeded pair on the experiment's
    source (idempotent), marks lessons injected into a newly failed run
    ineffective when the same failure reason recurred, and demotes stale
    lessons. Returns the lessons created by this call.
    """
    conn = init_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        if row is None or row["status"] not in _TERMINAL_STATUSES:
            return []
        experiment = dict(row)
        created: list[dict[str, Any]] = []
        if experiment.get("source_type") and experiment.get("source_id"):
            siblings = [
                dict(r)
                for r in conn.execute(
                    """SELECT * FROM experiments WHERE source_type=? AND source_id=?
                       ORDER BY created_at, id""",
                    (experiment["source_type"], experiment["source_id"]),
                ).fetchall()
            ]
            failures = [r for r in siblings if r["status"] in FAILURE_STATUSES]
            successes = [r for r in siblings if r["status"] == STATUS_SUCCEEDED]
            lessoned = {
                r[0]
                for r in conn.execute("SELECT failure_experiment_id FROM experiment_lessons")
            }
            for failure in failures:
                if failure["id"] in lessoned:
                    continue
                later = [
                    s for s in successes
                    if _parse_ts(s["created_at"]) > _parse_ts(failure["created_at"])
                ]
                if not later:
                    continue
                lesson = record_lesson(conn, failure, later[0], llm=llm, now=now)
                if lesson is not None:
                    created.append(lesson)
        if experiment["status"] in FAILURE_STATUSES:
            _mark_repeated_failures(conn, experiment, now=now)
        demote_stale_lessons(conn, now=now)
        conn.commit()
        return created
    finally:
        conn.close()


# ---- use: selection + prompt section ---------------------------------------


def _match_score(
    lesson: dict[str, Any], text_low: str, text_tokens: set[str], files: list[str]
) -> int:
    file_score = 0
    for candidate in files:
        if any(_file_match(candidate, lesson_file) for lesson_file in _lesson_files(lesson)):
            file_score += 4
    file_score = min(file_score, 8)
    keyword_score = sum(
        2 for keyword in lesson.get("keywords", ()) if keyword in text_tokens or keyword in text_low
    )
    keyword_score = min(keyword_score, 10)
    match = file_score + keyword_score
    if match <= 0:
        return 0  # no file/keyword overlap: the lesson does not match at all
    score = match
    if lesson.get("injected_count", 0) == 0:
        score += 1  # untried lessons get a chance
    score -= 2 * lesson.get("ineffective_count", 0)
    return score


def select_lessons(
    conn: sqlite3.Connection,
    text: str,
    files: Iterable[str] = (),
    *,
    limit: int = MAX_LESSONS_PER_PROMPT,
    now: str | None = None,
) -> list[dict[str, Any]]:
    """The up-to-*limit* active lessons best matching *text* and *files*.

    File matches score highest, then keyword overlap with the issue text,
    then untried lessons; ineffective lessons are penalized. Only lessons with
    a positive score match at all. Stale lessons are demoted lazily here.
    """
    ensure_schema(conn)
    demote_stale_lessons(conn, now=now)
    limit = max(1, min(int(limit), MAX_LESSONS_PER_PROMPT))
    text_low = (text or "").lower()
    text_tokens = set(_WORD_RE.findall(text_low))
    file_list = [str(f).strip() for f in (files or ()) if str(f).strip()]
    rows = conn.execute(
        "SELECT * FROM experiment_lessons WHERE status=?", (STATUS_ACTIVE,)
    ).fetchall()
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        lesson = _row_to_lesson(row)
        score = _match_score(lesson, text_low, text_tokens, file_list)
        if score > 0:
            scored.append((score, lesson))
    scored.sort(key=lambda item: (item[0], item[1]["created_at"]), reverse=True)
    return [lesson for _, lesson in scored[:limit]]


def render_lessons_section(
    lessons: list[dict[str, Any]], *, max_chars: int = LESSONS_SECTION_MAX_CHARS
) -> str:
    """The bounded "Lessons from earlier runs" prompt section; '' when empty."""
    if not lessons:
        return ""
    lines = [f"{LESSONS_SECTION_TITLE}:"]
    for lesson in lessons:
        line = f"- {_clean_rule(lesson.get('rule') or lesson.get('cause') or '')}"
        files = _lesson_files(lesson)
        if files:
            line += f" (files: {', '.join(files[:4])})"
        lines.append(line)
    section = "\n".join(lines)
    if len(section) > max_chars:
        section = section[: max_chars - 3] + "..."
    return section


def mark_injected(
    conn: sqlite3.Connection,
    lesson_ids: Iterable[str],
    experiment_id: str,
    *,
    now: str | None = None,
) -> None:
    """Record that *experiment_id*'s coding prompt showed these lessons."""
    ensure_schema(conn)
    stamp = now or _utcnow_iso()
    with conn:
        for lesson_id in lesson_ids:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO experiment_lesson_injections
                   (lesson_id, experiment_id, injected_at) VALUES (?, ?, ?)""",
                (lesson_id, experiment_id, stamp),
            )
            if cursor.rowcount:
                conn.execute(
                    """UPDATE experiment_lessons
                       SET injected_count = injected_count + 1, last_injected_at=?
                       WHERE id=?""",
                    (stamp, lesson_id),
                )


def lessons_for_prompt(
    db_path: str,
    *,
    goal: str,
    files: Iterable[str] = (),
    experiment_id: str | None = None,
    limit: int = MAX_LESSONS_PER_PROMPT,
    text: str | None = None,
) -> tuple[str, list[str]]:
    """The lessons section for one coding run and the matched lesson ids.

    Returns ``("", [])`` when nothing matches, so the prompt is unchanged.
    When *experiment_id* is given, the injection is tracked (once per lesson
    per experiment).
    """
    conn = init_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        lessons = select_lessons(
            conn, text if text is not None else goal, files, limit=limit
        )
        section = render_lessons_section(lessons)
        ids = [lesson["id"] for lesson in lessons]
        if ids and experiment_id:
            mark_injected(conn, ids, experiment_id)
        conn.commit()
        return section, ids
    finally:
        conn.close()


# ---- phrasing with the local model ------------------------------------------


def _local_model_phrase(prompt: str, db_path: str, session_id: str) -> str:
    import asyncio

    from modules.llama_cpp_client import LlamaCppClient
    from modules.local_llm_jobs import (
        ROLE_EXPERIMENT_LESSON,
        acquire_local_llm_job_service,
        release_local_llm_job_service,
    )

    client = LlamaCppClient()
    jobs = acquire_local_llm_job_service(db_path=db_path, llama_client=client)
    try:
        job_id = jobs.submit(
            prompt,
            system="Reply with exactly one short line. No markdown, no commentary.",
            temperature=0.0,
            max_tokens=120,
            session_id=session_id,
            role=ROLE_EXPERIMENT_LESSON,
        )
        return asyncio.run(jobs.wait(job_id))
    finally:
        release_local_llm_job_service(jobs)


def phrase_pending_lessons(
    limit: int = 3,
    *,
    llm: Callable[[str], Any] | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> list[str]:
    """Phrase up to *limit* unphrased active lessons with the local model.

    The finding text stored at record time stays as the rule whenever the
    model is unavailable or its reply unusable. Returns the phrased lesson ids.
    """
    if limit <= 0:
        return []
    conn = init_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT * FROM experiment_lessons
               WHERE phrased=0 AND status=? ORDER BY created_at LIMIT ?""",
            (STATUS_ACTIVE, int(limit)),
        ).fetchall()
        phrased: list[str] = []
        for row in rows:
            lesson = _row_to_lesson(row)
            prompt = _phrasing_prompt(
                lesson["cause"], _lesson_files(lesson), lesson.get("failure_kind") or "other"
            )
            try:
                raw = llm(prompt) if llm is not None else _local_model_phrase(prompt, db_path, lesson["id"])
                rule = _clean_rule(raw)
                if not rule:
                    continue
                with conn:
                    conn.execute(
                        """UPDATE experiment_lessons SET rule=?, phrased=1, model=?
                           WHERE id=? AND phrased=0""",
                        (
                            rule,
                            str(getattr(llm, "model", "local")) if llm is not None else "local",
                            lesson["id"],
                        ),
                    )
                phrased.append(lesson["id"])
            except Exception as exc:  # noqa: BLE001 - fallback rule stays, sweep continues
                logger.warning("experiment_lessons: phrasing lesson %s failed: %s", lesson["id"], exc)
        conn.commit()
        return phrased
    finally:
        conn.close()


# ---- read side ---------------------------------------------------------------
# Readers never create the tables: a database without them simply has no lessons.


def list_lessons(
    limit: int = 20,
    *,
    db_path: str = DEFAULT_DB_PATH,
    include_removed: bool = False,
) -> list[dict[str, Any]]:
    """Up to *limit* stored lessons, newest first (removed ones only on request)."""
    if limit <= 0:
        return []
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
    try:
        sql = "SELECT * FROM experiment_lessons"
        params: tuple[Any, ...] = ()
        if not include_removed:
            sql += " WHERE status != ?"
            params = (STATUS_REMOVED,)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        cursor = conn.execute(sql, (*params, min(int(limit), 500)))
        columns = [d[0] for d in cursor.description]
        return [_row_to_lesson(dict(zip(columns, row))) for row in cursor.fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
