"""Lessons store: what a failed-then-fixed experiment pair teaches the next run (#715).

Nexus repeats mistakes it has already solved once (wall-clock-dependent tests
were reverted in #675 and fixed in #681/#689; the ``AGENT_API_KEY`` ``setdefault``
problem blocked four experiments in #713). That knowledge exists only as
experiment/adoption rows, so it never reaches the next coding run. This module
closes the loop:

1. Record: when an experiment on some source (usually a GitHub issue) has both
   a failure and a later success, exactly one lesson is stored per
   failed-then-succeeded pair: ``cause`` -> ``rule`` with the evidence experiment
   ids, the touched files and keywords. A failure is a terminal run status
   (test/gate/review/timeout) *or* a linked adoption outcome (a review can
   block the merge while the experiment row still says ``succeeded``); a
   success is ``succeeded`` plus adopted (or not yet adopted at all). The rule
   is phrased with the local model when one is available
   (:func:`phrase_pending_lessons`); the sanitized finding text is the
   immediate fallback. All evidence is redacted before it is truncated or
   turned into keywords, so no secret or absolute path can leak into a stored
   lesson or a later paid prompt.
2. Use: every new coding prompt (initial implementation, verification repair,
   timing repair, local review fix) gets the up-to-5 best-matching lessons as a
   short "Lessons from earlier runs" section (:func:`lessons_for_prompt`),
   rendered as untrusted advisory context and fitted whole-entry into the
   prompt budget; the service runs the goal policy over the section and only
   tracks the injections that were actually sent to a worker.
3. Clean up: a lesson whose injected failure signature recurred is counted
   ineffective (each counted event is recorded atomically with its counter
   update, so replays never double-count) and demoted at the threshold; a
   lesson unused for ``STALE_UNUSED_DAYS`` is demoted; the store is
   hard-capped at ``MAX_LESSONS_TOTAL`` rows - insertion, eviction and the
   dependent-row cleanup happen in one transaction, and eviction keeps only a
   bounded dedup history so an evicted pair is not re-learned.
4. Visible: :func:`list_lessons` backs the ``lessons_list`` operation and the
   ``GET /lessons`` endpoint; :func:`failure_recurrence_stats` measures how
   often the same failure reason repeats per 20 experiments.

The status strings mirror ``modules.experiment_service.models`` and
``modules.experiment_adoption_parts.core``; they are kept local so this leaf
module never imports those packages (import cycle), and only ever compared as
strings.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from modules.redaction import redact, test_failure_tail
from modules.run_store import DEFAULT_DB_PATH, init_db

logger = logging.getLogger(__name__)

LESSONS_SECTION_TITLE = "Lessons from earlier runs"
# #715: 3-5 lessons per coding prompt; the selection limit is clamped to this.
MAX_LESSONS_PER_PROMPT = 5
# Hard cap on stored lessons (evicted rows are deleted, not accumulated).
MAX_LESSONS_TOTAL = 50
# Bounded dedup history of evicted (failure, success) pairs, so a pair that
# was evicted is not re-learned - but the history itself cannot grow forever.
EVICTED_PAIRS_MAX = 500
# Prompt-budget bound for the rendered section appended to a coding prompt.
LESSONS_SECTION_MAX_CHARS = 1600
MAX_RULE_CHARS = 300
MAX_CAUSE_CHARS = 500
MAX_FIX_CHARS = 800
MAX_KEYWORDS = 24
# A lesson that was injected but whose failure signature happened anyway this
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

# Adoption outcomes of modules.experiment_adoption_parts.core, duplicated as
# strings on purpose. A review can block the merge while the experiment row
# stays 'succeeded', so the linked adoption row is the authoritative outcome.
ADOPTION_ADOPTED = "adopted"
ADOPTION_FAILURE_STATUSES = ("failed", "needs_review")
ADOPTION_TERMINAL_STATUSES = (
    ADOPTION_ADOPTED, "failed", "needs_review", "interrupted", "ready_for_review",
)
_REVIEW_BLOCK_PREFIX = "Review by "

_WORD_RE = re.compile(r"[a-z0-9_./-]{3,}")
_PATH_TOKEN_RE = re.compile(r"(?:[\w.-]+/)+[\w.-]+")
_QUOTED_PATH_RE = re.compile(r"`([^`\n]+)`")
_STAT_LINE_RE = re.compile(r"^\s*(.+?)\s*\|\s*(?:\d+|Bin\b)")
_NUMSTAT_PARTS = 3
_SIG_MAX_TOKENS = 24
_BUSY_TIMEOUT_MS = 10_000

# Boilerplate and common words never become keywords and never take part in a
# failure signature: two unrelated GitHub issues share the heading
# "GitHub Issue #NNN: ..." and must not match on it (#715 review).
_STOPWORDS = frozenset(
    """
    a an and are as at be been but by for from has have if in into is it its of
    on or so that the their them then there these they this to was were will
    with we you your i
    github issue issues pr pull request module modules file files code repo
    repository implement implementation implementing implemented task tasks
    change changed changes changing fix fixed fixes fixing add adds added adding
    update updates updated updating remove removes removed removing make makes
    made making use uses used using need needs needed want wants wanted new via
    per agent nexus worker workers experiment experiments lesson lessons store
    feature refactor cleanup test tests testing spec specs please following
    instructions instruction description goal goals work working one two three
    failed failure fail error errors exception exceptions exit code traceback
    run ran runs
    """
    .split()
)


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

        -- one counted repeated-failure event per (lesson, experiment,
        -- signature): the PK is what makes replayed finalization safe, the
        -- counter update happens in the same transaction as the insert.
        CREATE TABLE IF NOT EXISTS experiment_lesson_events (
            lesson_id TEXT NOT NULL,
            experiment_id TEXT NOT NULL,
            signature TEXT NOT NULL,
            kind TEXT NULL,
            counted_at TEXT NOT NULL,
            PRIMARY KEY (lesson_id, experiment_id, signature)
        );

        CREATE INDEX IF NOT EXISTS idx_lesson_events_lesson
            ON experiment_lesson_events(lesson_id);

        -- bounded dedup history of evicted pairs
        CREATE TABLE IF NOT EXISTS experiment_lesson_evicted_pairs (
            failure_experiment_id TEXT NOT NULL,
            success_experiment_id TEXT NOT NULL,
            evicted_at TEXT NOT NULL,
            PRIMARY KEY (failure_experiment_id, success_experiment_id)
        );
        """
    )


def _require_schema(conn: sqlite3.Connection) -> None:
    """Ensure the tables (and the Row factory) exist *outside* any transaction.

    ``ensure_schema`` uses ``executescript``, which commits a pending
    transaction; calling it inside the atomic insert/evict block would break
    the all-or-nothing guarantee, so it only runs here, at entry points.
    """
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT 1 FROM experiment_lessons LIMIT 1")
        conn.execute("SELECT 1 FROM experiment_lesson_events LIMIT 1")
        conn.execute("SELECT 1 FROM experiment_lesson_evicted_pairs LIMIT 1")
    except sqlite3.OperationalError:
        ensure_schema(conn)


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


def _normalize_path(path: str) -> str:
    normalized = str(path).strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _changed_files(row: dict[str, Any], *, use_git: bool = True) -> list[str]:
    """Repo-relative paths the experiment touched.

    Parses the porcelain ``git_status`` and the stored ``diff_stat`` (both the
    ``--stat`` ``path | N +-`` format verification actually stores and the
    tab-separated ``--numstat`` format), and, when the worktree still exists,
    a reliable base-relative ``git diff --name-only <base>`` so committed
    checkpoint work is not missed either.
    """
    names: list[str] = []

    def add(path: str) -> None:
        path = _normalize_path(path).strip().strip('"')
        if path and path not in names:
            names.append(path)

    for line in (row.get("git_status") or "").splitlines():
        text = line.strip()
        if not text:
            continue
        name = text[2:].strip() if len(text) > 2 else text
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        add(name)
    for line in (row.get("diff_stat") or "").splitlines():
        stat_match = _STAT_LINE_RE.match(line)
        if stat_match:
            add(stat_match.group(1))
            continue
        parts = line.split("\t")
        if len(parts) >= _NUMSTAT_PARTS:
            add(parts[2])
    if use_git:
        worktree = str(row.get("worktree_path") or "")
        base = str(row.get("base_commit_sha") or "")
        if worktree and base and os.path.isdir(worktree):
            try:
                diff = subprocess.run(
                    ["git", "-C", worktree, "diff", "--name-only", "--no-renames", base],
                    capture_output=True, text=True, timeout=10,
                )
                if diff.returncode == 0:
                    for path in diff.stdout.splitlines():
                        add(path)
                untracked = subprocess.run(
                    ["git", "-C", worktree, "ls-files", "--others", "--exclude-standard"],
                    capture_output=True, text=True, timeout=10,
                )
                if untracked.returncode == 0:
                    for path in untracked.stdout.splitlines():
                        add(path)
            except (OSError, subprocess.SubprocessError):
                pass  # the stored status/stat parse above still applies
    return names[:100]


def _goal_files(text: str, *, limit: int = 20) -> list[str]:
    """Path-like tokens mentioned in the issue/goal text (``a/b.py``, `` `c.py` ``)."""
    found: list[str] = []

    def add(path: str) -> None:
        path = _normalize_path(path)
        if path and path not in found:
            found.append(path)

    for match in _PATH_TOKEN_RE.findall(text or ""):
        add(match)
    for match in _QUOTED_PATH_RE.findall(text or ""):
        add(match)
    return found[:limit]


def _text_tokens(text_low: str) -> set[str]:
    """Whole path-like tokens plus their word parts, for exact-token matching."""
    tokens: set[str] = set()
    for token in _WORD_RE.findall(text_low or ""):
        token = token.strip("./-")
        if token:
            tokens.add(token)
        for part in re.split(r"[./\-_]+", token):
            if len(part) >= 3:
                tokens.add(part)
    return tokens


def _keywords(text: str, files: Iterable[str] = ()) -> list[str]:
    """Significant lowercased words of *text* plus file basenames.

    *text* must already be redacted; boilerplate and common words (``github``,
    ``issue``, ``test``, ...) are dropped so unrelated issues do not match on
    their shared heading.
    """
    seen: set[str] = set()
    words: list[str] = []
    for token in re.findall(r"[a-z0-9][a-z0-9_+-]*", (text or "").lower()):
        if (
            len(token) >= 4
            and not token.isdigit()
            and token not in _STOPWORDS
            and token not in seen
        ):
            seen.add(token)
            words.append(token)
    for path in files or ():
        base = str(path).rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0].lower()
        if len(stem) >= 3 and stem not in _STOPWORDS and stem not in seen:
            seen.add(stem)
            words.append(stem)
    return words[:MAX_KEYWORDS]


def _signature(text: str) -> str:
    """Normalized failure signature: redacted, significant, sorted-deduped tokens.

    Generic words ("tests failed (exit code 1)") leave an empty signature and
    therefore never match; identical short reasons match by equality no matter
    how few qualifying words they have.
    """
    cleaned = redact(str(text or "")).lower()
    tokens: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[a-z0-9][a-z0-9_+-]*", cleaned):
        if (
            len(token) >= 4
            and not token.isdigit()
            and token not in _STOPWORDS
            and token not in seen
        ):
            seen.add(token)
            tokens.append(token)
        if len(tokens) >= _SIG_MAX_TOKENS:
            break
    return " ".join(sorted(tokens))


def _signatures_match(cause_sig: str, event_sig: str) -> bool:
    """Whether an event repeats the failure a lesson warns about.

    Equal signatures always match; otherwise a short event whose tokens are
    all contained in the cause signature matches too (the same reason, less
    boilerplate). Generic overlap alone does not match.
    """
    if not cause_sig or not event_sig:
        return False
    cause_tokens = set(cause_sig.split())
    event_tokens = set(event_sig.split())
    if cause_tokens == event_tokens:
        return True
    smaller, larger = (
        (event_tokens, cause_tokens)
        if len(event_tokens) <= len(cause_tokens)
        else (cause_tokens, event_tokens)
    )
    return len(smaller) >= 2 and smaller <= larger


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


def _quality_reason(row: dict[str, Any]) -> str:
    quality = row.get("quality_evaluation_json")
    if not quality:
        return ""
    try:
        parsed = json.loads(quality) or {}
    except (TypeError, json.JSONDecodeError):
        return ""
    reason = parsed.get("reason")
    findings = parsed.get("review_findings")
    if isinstance(findings, list):
        findings = "; ".join(str(f) for f in findings)
    text = "; ".join(str(part) for part in (reason, findings) if part)
    return redact(text).strip()


def _cause_text(row: dict[str, Any]) -> str:
    """Bounded, redacted failure diagnostics: the error, the gate/review
    finding, and the tail of the stored test output - never just the generic
    ``tests failed (exit code 1)`` line. Redaction happens before truncation."""
    parts: list[str] = []
    error = redact(str(row.get("error") or "")).strip()
    if error:
        parts.append(error)
    quality_reason = _quality_reason(row)
    if quality_reason and quality_reason not in error:
        parts.append(quality_reason)
    if row.get("test_exit_code") not in (None, 0):
        tail = "\n".join(test_failure_tail(row.get("test_output")))
        if tail and tail not in error:
            parts.append(tail)
    if not parts:
        parts.append(f"experiment ended as {row.get('status') or 'unknown'}")
    text = "\n".join(parts)
    if len(text) <= MAX_CAUSE_CHARS:
        return text
    return text[: MAX_CAUSE_CHARS - 3] + "..."


def _fix_text(success_row: dict[str, Any]) -> str:
    """A bounded, redacted description of what the successful attempt changed:
    the touched files, the diff stat and a few changed lines. This is the
    evidence the phrasing model gets about the actual fix."""
    files = _changed_files(success_row)
    lines: list[str] = []
    if files:
        lines.append("files: " + ", ".join(files[:10]))
    stat = [
        line.strip()
        for line in (success_row.get("diff_stat") or "").splitlines()
        if "|" in line or "file changed" in line
    ][:5]
    if stat:
        lines.append("stat: " + " ; ".join(stat))
    diff = str(success_row.get("diff_text") or "")
    changed = [
        line
        for line in diff.splitlines()
        if (line.startswith("+") or line.startswith("-"))
        and not line.startswith(("+++", "---"))
    ][:8]
    if changed:
        lines.append(
            "sample changed lines: "
            + " | ".join(redact(line)[:120] for line in changed)
        )
    text = redact("\n".join(lines)).strip()
    if not text:
        return "(no diff recorded)"
    return text if len(text) <= MAX_FIX_CHARS else text[: MAX_FIX_CHARS - 3] + "..."


def _clean_rule(raw: Any) -> str:
    """One short plain, redacted line out of a model reply; '' when unusable."""
    text = str(raw or "").strip()
    if not text:
        return ""
    line = text.splitlines()[0].strip()
    line = re.sub(r"^\d+[.)]\s*", "", line)
    line = line.lstrip("-#* ").strip().strip("`").strip('"').strip("'").strip()
    line = redact(line)
    if len(line) < 8:
        return ""
    return line if len(line) <= MAX_RULE_CHARS else line[: MAX_RULE_CHARS - 3] + "..."


def _safe_rule_text(raw: Any) -> str:
    """Render-side validation of a stored rule: untrusted advisory text is
    redacted and bounded again here, so a stale or tampered row cannot push
    raw evidence or secrets into a paid coding prompt."""
    return _clean_rule(raw)


def _phrasing_prompt(cause: str, files: list[str], failure_kind: str, fix: str) -> str:
    return (
        "A coding experiment failed and a later attempt on the same issue fixed it. "
        "Turn this into one short rule for future coding runs.\n"
        f"Failure ({failure_kind}): {cause}\n"
        f"What the successful attempt changed: {fix}\n"
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
    """Explicit path match: equal, same basename, or path suffix of either."""
    a = _normalize_path(candidate)
    b = _normalize_path(lesson_file)
    if not a or not b:
        return False
    return (
        a == b
        or a.rsplit("/", 1)[-1] == b.rsplit("/", 1)[-1]
        or a.endswith("/" + b)
        or b.endswith("/" + a)
    )


def _source_files(
    conn: sqlite3.Connection,
    source_type: str,
    source_id: str,
    exclude_id: str,
    *,
    limit: int = 6,
) -> list[str]:
    """Files earlier experiments on the same source touched (retry context)."""
    rows = conn.execute(
        """SELECT git_status, diff_stat, worktree_path, base_commit_sha
           FROM experiments
           WHERE source_type=? AND source_id=? AND id != ?
           ORDER BY created_at DESC, id DESC LIMIT ?""",
        (source_type, source_id, exclude_id, max(1, int(limit))),
    ).fetchall()
    files: list[str] = []
    for row in rows:
        for path in _changed_files(dict(row), use_git=False):
            if path not in files:
                files.append(path)
    return files[:20]


# ---- record ---------------------------------------------------------------


def _evict_locked(
    conn: sqlite3.Connection, *, now: str, cap: int, evicted_max: int = EVICTED_PAIRS_MAX
) -> int:
    """Enforce the hard cap *inside the caller's transaction*.

    Evicted lessons are deleted together with their injection and event rows,
    and their (failure, success) pair is remembered in a bounded dedup table.
    Must be called with ``with conn:`` already active and the schema ensured;
    never calls ``ensure_schema`` itself (``executescript`` would commit the
    pending insert mid-transaction).
    """
    cap = max(1, int(cap))
    rows = conn.execute(
        """SELECT id, failure_experiment_id, success_experiment_id FROM experiment_lessons
           WHERE status != ?
           ORDER BY injected_count ASC, COALESCE(last_injected_at, created_at) ASC,
                    created_at ASC""",
        (STATUS_REMOVED,),
    ).fetchall()
    excess = len(rows) - cap
    if excess <= 0:
        return 0
    for row in rows[:excess]:
        lesson_id, failure_id, success_id = row[0], row[1], row[2]
        conn.execute(
            "DELETE FROM experiment_lesson_injections WHERE lesson_id=?", (lesson_id,)
        )
        conn.execute(
            "DELETE FROM experiment_lesson_events WHERE lesson_id=?", (lesson_id,)
        )
        conn.execute(
            """INSERT OR IGNORE INTO experiment_lesson_evicted_pairs
               (failure_experiment_id, success_experiment_id, evicted_at)
               VALUES (?, ?, ?)""",
            (failure_id, success_id, now),
        )
        conn.execute("DELETE FROM experiment_lessons WHERE id=?", (lesson_id,))
    # Bound the dedup history itself.
    conn.execute(
        """DELETE FROM experiment_lesson_evicted_pairs WHERE rowid NOT IN (
               SELECT rowid FROM experiment_lesson_evicted_pairs
               ORDER BY evicted_at DESC, rowid DESC LIMIT ?)""",
        (max(1, int(evicted_max)),),
    )
    return excess


def enforce_hard_cap(
    conn: sqlite3.Connection, *, now: str | None = None, cap: int = MAX_LESSONS_TOTAL
) -> int:
    """Keep at most *cap* non-removed lessons; evict the least valuable ones.

    The eviction and the dependent-row cleanup run in one transaction, so an
    error or interruption can never leave the cap exceeded. Returns how many
    rows were evicted.
    """
    _require_schema(conn)
    stamp = now or _utcnow_iso()
    with conn:
        return _evict_locked(conn, now=stamp, cap=cap)


def record_lesson(
    conn: sqlite3.Connection,
    failure_row: dict[str, Any],
    success_row: dict[str, Any],
    *,
    llm: Callable[[str], Any] | None = None,
    now: str | None = None,
    kind: str | None = None,
    cause: str | None = None,
    cap: int = MAX_LESSONS_TOTAL,
) -> dict[str, Any] | None:
    """Store exactly one lesson for a failed-then-succeeded pair.

    Idempotent: an existing lesson for the pair is returned unchanged. The
    insert and the hard-cap eviction (with dependent-row cleanup) happen in a
    single transaction, so a crash halfway leaves no partial state. *llm*,
    when given, phrases the rule (``llm(prompt) -> text``) with the failure
    diagnostics and a bounded description of the fix; any model error keeps
    the sanitized finding text as the fallback rule.
    """
    _require_schema(conn)
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
    failure_kind = kind or _failure_kind(failure_row)
    cause_text = cause if cause is not None else _cause_text(failure_row)
    fix = _fix_text(success_row)
    keywords = _keywords(
        f"{redact(str(failure_row.get('goal') or ''))} {cause_text} "
        f"{redact(str(success_row.get('goal') or ''))}",
        files,
    )
    rule = cause_text[:MAX_RULE_CHARS]
    phrased = 0
    model: str | None = None
    if llm is not None:
        try:
            candidate = _clean_rule(llm(_phrasing_prompt(cause_text, files, failure_kind, fix)))
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
                failure_id, success_id, failure_kind, cause_text, rule, phrased, model,
                json.dumps(files, ensure_ascii=False), json.dumps(keywords, ensure_ascii=False),
            ),
        )
        # Enforced in the same transaction so the store never exceeds the cap,
        # even when this very row is the one that crosses it.
        _evict_locked(conn, now=stamp, cap=cap)
    row = conn.execute(
        "SELECT * FROM experiment_lessons WHERE failure_experiment_id=? AND success_experiment_id=?",
        (failure_id, success_id),
    ).fetchone()
    return _row_to_lesson(row) if row is not None else None


# ---- failure events (recurrence accounting) ---------------------------------


def _failure_events(
    conn: sqlite3.Connection, experiment: dict[str, Any], adoption: dict[str, Any] | None
) -> list[tuple[str, str]]:
    """The actual review/gate/test events of one experiment, as (kind, text).

    Includes every failed verification iteration (a failure later repaired
    within the same experiment counts too), the experiment row's own finding,
    and the linked adoption outcome (e.g. a review-blocked merge).
    """
    events: list[tuple[str, str]] = []
    experiment_id = experiment["id"]
    try:
        runs = conn.execute(
            """SELECT status, exit_code, error, output FROM verification_runs
               WHERE experiment_id=? ORDER BY iteration""",
            (experiment_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        runs = []
    for run in runs:
        status = run["status"] if not isinstance(run, dict) else run.get("status")
        exit_code = run["exit_code"] if not isinstance(run, dict) else run.get("exit_code")
        error = run["error"] if not isinstance(run, dict) else run.get("error")
        output = run["output"] if not isinstance(run, dict) else run.get("output")
        if status not in ("failed", "needs_review", "hung_test"):
            continue
        if not error and exit_code in (None, 0):
            continue
        kind = "test" if exit_code not in (None, 0) else (
            "hung_test" if status == "hung_test" else "gate"
        )
        text = f"{error or ''}\n" + "\n".join(test_failure_tail(output))
        events.append((kind, text))
    if experiment.get("status") in FAILURE_STATUSES:
        events.append((_failure_kind(experiment), _cause_text(experiment)))
    if adoption and adoption.get("status") in ADOPTION_FAILURE_STATUSES:
        reason = str(adoption.get("reason") or "").strip()
        if reason:
            kind = "review" if reason.startswith(_REVIEW_BLOCK_PREFIX) else "adoption"
            events.append((kind, reason))
    # dedupe identical signatures later; keep event order stable
    seen_sigs: set[str] = set()
    unique: list[tuple[str, str]] = []
    for kind, text in events:
        sig = _signature(text)
        if sig and sig in seen_sigs:
            continue
        if sig:
            seen_sigs.add(sig)
        unique.append((kind, text))
    return unique


def record_failure_event(
    conn: sqlite3.Connection,
    lesson_id: str,
    experiment_id: str,
    kind: str,
    signature: str,
    *,
    now: str | None = None,
) -> str | None:
    """Record one repeated-failure event and bump the counter atomically.

    The ``INSERT OR IGNORE`` plus the counter update happen in one
    transaction keyed by (lesson, experiment, signature), so replaying
    finalization never counts the same event twice. Demotes at the threshold.
    Returns the lesson's status after the update (None when the lesson is gone).
    """
    _require_schema(conn)
    stamp = now or _utcnow_iso()
    with conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO experiment_lesson_events
               (lesson_id, experiment_id, signature, kind, counted_at)
               VALUES (?, ?, ?, ?, ?)""",
            (lesson_id, experiment_id, signature, kind, stamp),
        )
        if not cursor.rowcount:
            row = conn.execute(
                "SELECT status FROM experiment_lessons WHERE id=?", (lesson_id,)
            ).fetchone()
            return str(row[0]) if row is not None else None
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
    return str(row[1]) if row is not None else None


def mark_ineffective(conn: sqlite3.Connection, lesson_id: str, *, now: str | None = None) -> str:
    """Count one repeated failure for *lesson_id*; demote at the threshold.

    Returns the lesson's status after the update.
    """
    _require_schema(conn)
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


def _count_failure_events(
    conn: sqlite3.Connection,
    experiment: dict[str, Any],
    adoption: dict[str, Any] | None,
    *,
    now: str | None = None,
) -> list[str]:
    """Count injected lessons whose failure signature recurred in *experiment*.

    Returns the lesson ids that were demoted by this call.
    """
    injected = [
        r[0]
        for r in conn.execute(
            "SELECT lesson_id FROM experiment_lesson_injections WHERE experiment_id=?",
            (experiment["id"],),
        ).fetchall()
    ]
    if not injected:
        return []
    placeholders = ",".join("?" for _ in injected)
    lessons = {
        row["id"]: _row_to_lesson(row)
        for row in conn.execute(
            f"SELECT * FROM experiment_lessons WHERE id IN ({placeholders})", injected
        ).fetchall()
    }
    events = _failure_events(conn, experiment, adoption)
    if not events:
        return []
    demoted: list[str] = []
    for lesson_id in injected:
        lesson = lessons.get(lesson_id)
        if lesson is None or lesson["status"] != STATUS_ACTIVE:
            continue
        cause_sig = _signature(lesson["cause"])
        if not cause_sig:
            continue
        for kind, text in events:
            sig = _signature(text)
            if sig and _signatures_match(cause_sig, sig):
                if record_failure_event(
                    conn, lesson_id, experiment["id"], kind, sig, now=now
                ) == STATUS_DEMOTED:
                    demoted.append(lesson_id)
    return demoted


def demote_stale_lessons(
    conn: sqlite3.Connection, *, now: str | None = None, stale_days: int = STALE_UNUSED_DAYS
) -> int:
    """Demote active lessons not injected for *stale_days*; returns the count."""
    _require_schema(conn)
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


# ---- reconciliation ----------------------------------------------------------


def _adoption_map(conn: sqlite3.Connection, experiment_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not experiment_ids:
        return {}
    placeholders = ",".join("?" for _ in experiment_ids)
    try:
        rows = conn.execute(
            f"SELECT * FROM experiment_adoptions WHERE experiment_id IN ({placeholders})",
            experiment_ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["experiment_id"]: dict(row) for row in rows}


def _reconcile(
    conn: sqlite3.Connection,
    experiment: dict[str, Any],
    *,
    llm: Callable[[str], Any] | None = None,
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Reconcile lessons for the experiment's source once an outcome exists.

    Reads the linked adoption outcomes (a review-blocked merge is a failure
    even while the experiment row says ``succeeded``), withdraws lessons whose
    "success" later failed at adoption, creates one lesson per
    failed-then-succeeded pair, counts repeated failure signatures of injected
    lessons, and demotes stale lessons. Idempotent; safe to replay.
    """
    stamp = now or _utcnow_iso()
    created: list[dict[str, Any]] = []
    source_type = experiment.get("source_type")
    source_id = experiment.get("source_id")
    adoption = _adoption_map(conn, [experiment["id"]]).get(experiment["id"])
    if source_type and source_id:
        siblings = [
            dict(r)
            for r in conn.execute(
                """SELECT * FROM experiments WHERE source_type=? AND source_id=?
                   ORDER BY created_at, id""",
                (source_type, source_id),
            ).fetchall()
        ]
        adoptions = _adoption_map(conn, [s["id"] for s in siblings])

        # A lesson whose success experiment later failed at adoption/review did
        # not hold; demote it instead of feeding a wrong rule forward.
        with conn:
            for row in conn.execute(
                """SELECT id, success_experiment_id FROM experiment_lessons
                   WHERE source_type=? AND source_id=? AND status=?""",
                (source_type, source_id, STATUS_ACTIVE),
            ).fetchall():
                success_adoption = adoptions.get(row["success_experiment_id"])
                if success_adoption and success_adoption.get("status") in ADOPTION_FAILURE_STATUSES:
                    conn.execute(
                        """UPDATE experiment_lessons SET status=?, demoted_at=COALESCE(demoted_at, ?)
                           WHERE id=? AND status=?""",
                        (STATUS_DEMOTED, stamp, row["id"], STATUS_ACTIVE),
                    )

        failures: list[tuple[dict[str, Any], str, str]] = []
        successes: list[dict[str, Any]] = []
        for sibling in siblings:
            sibling_adoption = adoptions.get(sibling["id"])
            if sibling["status"] in FAILURE_STATUSES:
                failures.append((sibling, _failure_kind(sibling), _cause_text(sibling)))
            elif sibling_adoption and sibling_adoption.get("status") in ADOPTION_FAILURE_STATUSES:
                reason = redact(str(sibling_adoption.get("reason") or "adoption stopped")).strip()
                reason = reason[:MAX_CAUSE_CHARS] if len(reason) > MAX_CAUSE_CHARS else reason
                kind = (
                    "review"
                    if str(sibling_adoption.get("reason") or "").startswith(_REVIEW_BLOCK_PREFIX)
                    else "adoption"
                )
                view = dict(sibling)
                view["error"] = reason
                failures.append((view, kind, reason))
            elif sibling["status"] == STATUS_SUCCEEDED and (
                sibling_adoption is None or sibling_adoption.get("status") == ADOPTION_ADOPTED
            ):
                successes.append(sibling)

        lessoned = {
            (r[0], r[1])
            for r in conn.execute(
                "SELECT failure_experiment_id, success_experiment_id FROM experiment_lessons"
            ).fetchall()
        }
        evicted = {
            (r[0], r[1])
            for r in conn.execute(
                "SELECT failure_experiment_id, success_experiment_id FROM experiment_lesson_evicted_pairs"
            ).fetchall()
        }
        for failure_row, kind, cause in failures:
            later = [
                s for s in successes
                if _parse_ts(s["created_at"]) > _parse_ts(failure_row["created_at"])
            ]
            if not later:
                continue
            success_row = later[0]
            pair = (str(failure_row["id"]), str(success_row["id"]))
            if pair in lessoned or pair in evicted:
                continue
            lesson = record_lesson(
                conn, failure_row, success_row, llm=llm, now=now, kind=kind, cause=cause
            )
            if lesson is not None:
                created.append(lesson)
                lessoned.add(pair)

    _count_failure_events(conn, experiment, adoption, now=now)
    demote_stale_lessons(conn, now=now)
    return created


def note_experiment_finished(
    db_path: str,
    experiment_id: str,
    *,
    llm: Callable[[str], Any] | None = None,
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Bookkeeping after an experiment reached a terminal status (#715).

    Reconciles lessons for the experiment's source using both the experiment
    rows and the linked adoption outcomes (review-blocked merges included),
    counts repeated failure signatures of lessons injected into this run, and
    demotes stale lessons. Idempotent. Returns the lessons created by this
    call.
    """
    conn = init_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        if row is None or row["status"] not in _TERMINAL_STATUSES:
            return []
        return _reconcile(conn, dict(row), llm=llm, now=now)
    finally:
        conn.close()


def note_adoption_outcome(
    db_path: str,
    experiment_id: str,
    *,
    llm: Callable[[str], Any] | None = None,
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Reconcile when adoption (or re-verification) establishes the outcome (#715).

    The adoption reviewer can block a merge while the experiment row remains
    ``succeeded``; this entry point makes the lessons store see the linked
    adoption outcome and reason.
    """
    conn = init_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        if row is None:
            return []
        experiment = dict(row)
        adoption = _adoption_map(conn, [experiment_id]).get(experiment_id)
        adoption_terminal = adoption is not None and adoption.get("status") in ADOPTION_TERMINAL_STATUSES
        if experiment["status"] not in _TERMINAL_STATUSES and not adoption_terminal:
            return []
        return _reconcile(conn, experiment, llm=llm, now=now)
    finally:
        conn.close()


# ---- use: selection + prompt section ---------------------------------------


def _match_score(
    lesson: dict[str, Any], text_tokens: set[str], files: list[str]
) -> int:
    file_score = 0
    for candidate in files:
        if any(_file_match(candidate, lesson_file) for lesson_file in _lesson_files(lesson)):
            file_score += 4
    file_score = min(file_score, 8)
    # Exact-token keyword match only: no substring matching, so two unrelated
    # issues do not match on shared boilerplate.
    keyword_score = sum(
        2 for keyword in lesson.get("keywords", ()) if keyword in text_tokens
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

    File matches score highest, then exact-token keyword overlap with the
    issue text, then untried lessons; ineffective lessons are penalized. Only
    lessons with a positive score match at all. Stale lessons are demoted
    lazily here.
    """
    _require_schema(conn)
    demote_stale_lessons(conn, now=now)
    limit = max(1, min(int(limit), MAX_LESSONS_PER_PROMPT))
    text_tokens = _text_tokens((text or "").lower())
    file_list = [_normalize_path(f) for f in (files or ()) if _normalize_path(f)]
    rows = conn.execute(
        "SELECT * FROM experiment_lessons WHERE status=?", (STATUS_ACTIVE,)
    ).fetchall()
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        lesson = _row_to_lesson(row)
        score = _match_score(lesson, text_tokens, file_list)
        if score > 0:
            scored.append((score, lesson))
    scored.sort(key=lambda item: (item[0], item[1]["created_at"]), reverse=True)
    return [lesson for _, lesson in scored[:limit]]


def render_lessons_section(
    lessons: list[dict[str, Any]], *, max_chars: int = LESSONS_SECTION_MAX_CHARS
) -> tuple[str, list[dict[str, Any]]]:
    """The bounded "Lessons from earlier runs" prompt section.

    Returns ``(section, sent_lessons)``. Complete entries are fitted into
    *max_chars* (highest-scored first); an entry that does not fit is left out
    entirely - the section is never truncated mid-entry - and only the
    entries actually rendered are returned, so only those can be tracked as
    injected. The header marks the section as untrusted advisory context.
    ``("", [])`` when nothing fits or nothing is given.
    """
    if not lessons:
        return "", []
    header = (
        f"{LESSONS_SECTION_TITLE} (untrusted advisory context from earlier runs; "
        "the task instructions above take precedence):"
    )
    entries: list[tuple[str, dict[str, Any]]] = []
    for lesson in lessons:
        rule = _safe_rule_text(lesson.get("rule") or lesson.get("cause") or "")
        if not rule:
            continue
        line = f"- {rule}"
        files = _lesson_files(lesson)
        if files:
            line += f" (files: {', '.join(files[:4])})"
        entries.append((line, lesson))
    section = header
    sent: list[dict[str, Any]] = []
    for line, lesson in entries:
        candidate = f"{section}\n{line}"
        if len(candidate) > max_chars:
            break
        section = candidate
        sent.append(lesson)
    if not sent:
        return "", []
    return section, sent


def mark_injected(
    conn: sqlite3.Connection,
    lesson_ids: Iterable[str],
    experiment_id: str,
    *,
    now: str | None = None,
) -> None:
    """Record that *experiment_id*'s coding prompt was sent with these lessons."""
    _require_schema(conn)
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


def mark_injected_for(
    db_path: str,
    lesson_ids: Iterable[str],
    experiment_id: str,
    *,
    now: str | None = None,
) -> None:
    """Path-based :func:`mark_injected` for callers that hold no connection."""
    ids = [str(i) for i in (lesson_ids or ()) if str(i).strip()]
    if not ids:
        return
    conn = init_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        mark_injected(conn, ids, experiment_id, now=now)
    finally:
        conn.close()


def lessons_for_prompt(
    db_path: str,
    *,
    goal: str,
    files: Iterable[str] = (),
    experiment_id: str | None = None,
    limit: int = MAX_LESSONS_PER_PROMPT,
    text: str | None = None,
    max_chars: int = LESSONS_SECTION_MAX_CHARS,
) -> tuple[str, list[str]]:
    """The lessons section for one coding run and the ids actually rendered.

    Returns ``("", [])`` when nothing matches, so the prompt is unchanged.
    The target files come from the plan, the issue/goal text and earlier
    experiments on the same source (retry context). This function never marks
    injections: only the entries returned here were sent, and the caller marks
    them right before dispatching the worker.
    """
    conn = init_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        all_files = [_normalize_path(f) for f in (files or ()) if _normalize_path(f)]
        all_files.extend(f for f in _goal_files(goal) if f not in all_files)
        if experiment_id:
            row = conn.execute(
                "SELECT source_type, source_id FROM experiments WHERE id=?", (experiment_id,)
            ).fetchone()
            if row is not None and row["source_type"] and row["source_id"]:
                for path in _source_files(
                    conn, row["source_type"], row["source_id"], experiment_id
                ):
                    if path not in all_files:
                        all_files.append(path)
        lessons = select_lessons(
            conn, text if text is not None else goal, all_files, limit=limit
        )
        section, sent = render_lessons_section(lessons, max_chars=max_chars)
        return section, [lesson["id"] for lesson in sent]
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

    The prompt carries the sanitized failure diagnostics and a bounded
    description of the fix; the model reply is validated (one plain redacted
    line) before it replaces the fallback rule, which stays whenever the model
    is unavailable or its reply unusable. Returns the phrased lesson ids.
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
            success = conn.execute(
                "SELECT * FROM experiments WHERE id=?",
                (lesson["success_experiment_id"],),
            ).fetchone()
            fix = _fix_text(dict(success)) if success is not None else "(no diff recorded)"
            prompt = _phrasing_prompt(
                lesson["cause"], _lesson_files(lesson),
                lesson.get("failure_kind") or "other", fix,
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


def failure_recurrence_stats(db_path: str, *, window: int = 20) -> dict[str, Any]:
    """How often the same failure reason repeats per *window* experiments (#715 measure).

    Counts failure events (failed verification iterations, terminal failure
    findings, review-blocked adoptions) of the newest *window* experiments and
    how many of them repeat a signature already seen on the same source earlier
    in the window. Read-only; returns zeros when the store is absent.
    """
    stats = {
        "window": 0,
        "failure_events": 0,
        "repeated": 0,
        "repeat_rate": 0.0,
    }
    window = max(1, min(int(window), 200))
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT * FROM experiments ORDER BY created_at DESC, id DESC LIMIT ?""",
            (window,),
        ).fetchall()
        adoptions = _adoption_map(conn, [row["id"] for row in rows])
        seen: dict[tuple[str, str], set[str]] = {}
        repeated = 0
        total = 0
        for row in reversed(rows):  # oldest first, so "earlier" means earlier
            experiment = dict(row)
            events = _failure_events(conn, experiment, adoptions.get(experiment["id"]))
            source_key = (str(experiment.get("source_type") or ""), str(experiment.get("source_id") or ""))
            for _kind, text in events:
                sig = _signature(text)
                if not sig:
                    continue
                total += 1
                bucket = seen.setdefault(source_key, set())
                if sig in bucket:
                    repeated += 1
                else:
                    bucket.add(sig)
        stats.update(
            window=len(rows),
            failure_events=total,
            repeated=repeated,
            repeat_rate=round(repeated / total, 3) if total else 0.0,
        )
        return stats
    except sqlite3.OperationalError:
        return stats
    finally:
        conn.close()
