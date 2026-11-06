"""Persistent issue-approval registry (SQLite) - the authorization state for
external GitHub issues.

Architecture: **public GitHub = discovery, proposals and discussion; the
private mailbox = trusted approval commands; this local SQLite table =
persistent authorization state; Nexus = autonomous planning and execution of
approved issues only.**

An issue is executable only when this registry holds an ``approved`` row for
its ``(repository, issue_number)``. For issues whose author is not a trusted
account the ``content_hash`` must also match the issue's current title+body
(a stranger could edit their own issue after approval). Anything else - no
row (pending), ``revoked``, ``rejected``, a repository/issue mismatch, a
corrupted row, or (for untrusted authors) edited issue content - fails closed.
Approvals are recorded exclusively through structured ``issue_approval``
mailbox messages from the trusted private mailbox (see
``modules/mailbox/processing.py``); issue text, comments, labels and PRs can
never create or modify an approval.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "ExecutionVerdict",
    "IssueApproval",
    "ISSUE_APPROVALS_SCHEMA",
    "STATUS_APPROVED",
    "STATUS_REJECTED",
    "STATUS_REVOKED",
    "KNOWN_STATUSES",
    "check_execution",
    "ensure_issue_approvals_table",
    "get_approval",
    "issue_content_hash",
    "normalize_repository",
    "record_decision",
]

STATUS_APPROVED = "approved"
STATUS_REVOKED = "revoked"
STATUS_REJECTED = "rejected"
KNOWN_STATUSES = frozenset({STATUS_APPROVED, STATUS_REVOKED, STATUS_REJECTED})

# Same canonicalization the controller sees (title capped at 500, body at
# 16000 chars), so the hash computed at approval time matches the hash checked
# at dispatch time regardless of where the text was truncated.
_TITLE_CAP = 500
_BODY_CAP = 16000
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

ISSUE_APPROVALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS issue_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    source_mailbox_msg_id INTEGER,
    decided_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(repository, issue_number)
);
CREATE INDEX IF NOT EXISTS idx_issue_approvals_lookup
    ON issue_approvals(repository, issue_number);
"""


def ensure_issue_approvals_table(conn: sqlite3.Connection) -> None:
    """Create the approval table if missing (idempotent, additive)."""
    conn.executescript(ISSUE_APPROVALS_SCHEMA)
    conn.commit()


def normalize_repository(raw: Any) -> str:
    """Canonical ``owner/repo`` (lowercased); empty when malformed."""
    text = str(raw or "").strip().lower()
    if not text or text.count("/") != 1:
        return ""
    owner, repo = text.split("/", 1)
    if not owner or not repo:
        return ""
    return f"{owner}/{repo}"


def issue_content_hash(title: Any, body: Any) -> str:
    """Deterministic hash binding an approval to the issue title and body."""
    canonical_title = str(title or "")[:_TITLE_CAP]
    canonical_body = str(body or "")[:_BODY_CAP]
    digest = hashlib.sha256()
    digest.update(canonical_title.encode("utf-8"))
    digest.update(b"\n\n")
    digest.update(canonical_body.encode("utf-8"))
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class IssueApproval:
    repository: str
    issue_number: int
    status: str
    content_hash: str
    source_mailbox_msg_id: int | None
    decided_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_decision(
    conn: sqlite3.Connection,
    *,
    repository: Any,
    issue_number: Any,
    status: Any,
    content_hash: str = "",
    source_mailbox_msg_id: Any = None,
) -> tuple[IssueApproval, bool]:
    """Record an approval decision transactionally and idempotently.

    Returns ``(approval, changed)``. Re-delivering the same decision (same
    status, content hash and source message) changes nothing and returns
    ``changed=False``, so duplicate mailbox messages are safe. A row for a
    different repository or issue number is a separate entry.
    """
    repo = normalize_repository(repository)
    if not repo:
        raise ValueError(f"invalid repository {repository!r} (expected 'owner/repo')")
    try:
        number = int(issue_number)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid issue number {issue_number!r}") from exc
    if isinstance(issue_number, bool) or number < 1:
        raise ValueError(f"invalid issue number {issue_number!r}")
    decision = str(status or "").strip().lower()
    if decision not in KNOWN_STATUSES:
        raise ValueError(f"unknown approval status {status!r} (allowed: {sorted(KNOWN_STATUSES)})")
    digest = str(content_hash or "").strip().lower()
    if decision == STATUS_APPROVED and not _HASH_RE.match(digest):
        raise ValueError("an approval requires a sha256 content hash of the issue title and body")
    msg_id = None
    if source_mailbox_msg_id is not None:
        msg_id = int(source_mailbox_msg_id)

    now = _now()
    existing = get_approval(conn, repo, number)
    if (
        existing is not None
        and existing.status == decision
        and existing.content_hash == digest
        and existing.source_mailbox_msg_id == msg_id
    ):
        return existing, False
    with conn:
        conn.execute(
            """
            INSERT INTO issue_approvals
                (repository, issue_number, status, content_hash,
                 source_mailbox_msg_id, decided_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repository, issue_number) DO UPDATE SET
                status = excluded.status,
                content_hash = excluded.content_hash,
                source_mailbox_msg_id = excluded.source_mailbox_msg_id,
                decided_at = excluded.decided_at,
                updated_at = excluded.updated_at
            """,
            (repo, number, decision, digest, msg_id, now, now),
        )
    row = get_approval(conn, repo, number)
    if row is None:  # pragma: no cover - the upsert cannot lose the row
        raise RuntimeError("approval row missing after insert")
    return row, True


def get_approval(
    conn: sqlite3.Connection, repository: Any, issue_number: Any
) -> IssueApproval | None:
    """The current approval row for one issue, or ``None`` when there is none."""
    repo = normalize_repository(repository)
    if not repo:
        return None
    try:
        number = int(issue_number)
    except (TypeError, ValueError):
        return None
    if number < 1:
        return None
    row = conn.execute(
        """
        SELECT repository, issue_number, status, content_hash,
               source_mailbox_msg_id, decided_at, updated_at
        FROM issue_approvals WHERE repository = ? AND issue_number = ?
        """,
        (repo, number),
    ).fetchone()
    if row is None:
        return None
    return IssueApproval(
        repository=str(row[0]),
        issue_number=int(row[1]),
        status=str(row[2]),
        content_hash=str(row[3] or ""),
        source_mailbox_msg_id=int(row[4]) if row[4] is not None else None,
        decided_at=str(row[5] or ""),
        updated_at=str(row[6] or ""),
    )


@dataclass(frozen=True, slots=True)
class ExecutionVerdict:
    """The centralized execution-boundary verdict for one issue."""

    allowed: bool
    reason: str
    approval: IssueApproval | None = None


def check_execution(
    conn: sqlite3.Connection,
    *,
    repository: Any,
    issue_number: Any,
    title: Any,
    body: Any,
    skip_content_hash: bool = False,
    trusted_author: bool = False,
) -> ExecutionVerdict:
    """May Nexus start new work on this issue right now?

    Fails closed on every anomaly: no row (pending), revoked/rejected,
    unknown repository or issue number, corrupted status or hash, or -
    unless ``skip_content_hash`` is set - a content hash that no longer
    matches the approved one (the issue was edited and needs renewed
    approval). The hash check is skipped only for issues whose author is a
    trusted actor: a stranger could edit their own issue after approval,
    while a trusted author's edit (e.g. a typo fix) must not invalidate it.
    An issue a trusted actor wrote needs no approval record: the owner's
    own issues (and released proposals) are approved by being written. An
    explicit revoked/rejected record still blocks it.
    """
    repo = normalize_repository(repository)
    try:
        number = int(issue_number)
    except (TypeError, ValueError):
        return ExecutionVerdict(False, f"invalid issue number {issue_number!r}")
    if not repo:
        return ExecutionVerdict(False, f"invalid repository {repository!r}")
    if number < 1:
        return ExecutionVerdict(False, f"invalid issue number {issue_number!r}")
    try:
        approval = get_approval(conn, repo, number)
    except sqlite3.Error as exc:
        return ExecutionVerdict(False, f"approval registry unreadable: {exc}")
    if approval is None:
        if trusted_author:
            return ExecutionVerdict(True, f"issue {repo}#{number} was written by a trusted actor")
        return ExecutionVerdict(False, f"issue {repo}#{number} has no approval record (pending)")
    if approval.status not in KNOWN_STATUSES:
        return ExecutionVerdict(
            False, f"corrupted approval record for {repo}#{number} (status {approval.status!r})",
            approval,
        )
    if approval.status != STATUS_APPROVED:
        return ExecutionVerdict(
            False, f"issue {repo}#{number} is {approval.status} (approved at {approval.decided_at})",
            approval,
        )
    if not _HASH_RE.match(approval.content_hash or ""):
        return ExecutionVerdict(
            False, f"corrupted approval record for {repo}#{number} (bad content hash)", approval
        )
    if not skip_content_hash:
        current_hash = issue_content_hash(title, body)
        if current_hash != approval.content_hash:
            return ExecutionVerdict(
                False,
                f"issue {repo}#{number} content changed since approval "
                f"(approved {approval.content_hash[:12]}, now {current_hash[:12]}); "
                "renewed approval required",
                approval,
            )
    return ExecutionVerdict(True, f"issue {repo}#{number} approved at {approval.decided_at}", approval)
