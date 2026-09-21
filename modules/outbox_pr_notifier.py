"""Publish Outbox messages as comments on the persistent communication PR (#196).

The ChatGPT Work trigger watches one long-lived *Nexus communication* pull
request (#194).  Every Outbox message that Nexus persists must therefore also
appear as a top-level conversation comment on that PR, so the external trigger
wakes up.

Contract
--------
- Delivery is keyed by Outbox message id: one message produces at most one
  comment, no matter how often it is reprocessed.
- A GitHub failure never rolls back or loses the Outbox message.  The
  notification stays ``pending`` and is retried with bounded exponential
  backoff by :meth:`OutboxPRNotifier.deliver_pending`.
- A closed communication PR is reopened (recreating its head branch first if
  GitHub deleted it) rather than replaced.
- Only when the original PR is genuinely unusable is an equivalent channel
  recreated - and that condition is logged at ERROR with the new number and
  URL, because the external trigger is still scoped to the old PR.
- The communication PR is never merged from here.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from modules.redaction import redact as _redact_full
from modules.run_store import DEFAULT_DB_PATH, init_db
from modules.run_store.records import get_setting, set_setting

logger = logging.getLogger(__name__)

OUTBOX_MARKER = "[NEXUS-OUTBOX]"

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_PR_NUMBER = 194
DEFAULT_COMMUNICATION_BRANCH = "nexus-communication"
DEFAULT_BASE_BRANCH = "main"
COMMUNICATION_FILE = "docs/nexus-communication.md"

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_BACKOFF_SECONDS = 30.0
# GitHub rejects comment bodies over 65536 characters.
MAX_BODY_CHARS = 60_000

STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"

# Persisted channel identity: survives a fallback recreation.
_PR_SETTING_KEY = "communication_pr_number"

Transport = Callable[[str, str, "dict[str, Any] | None"], Any]


class OutboxPRNotifyError(RuntimeError):
    """A GitHub call needed for publishing failed (always retryable)."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(value: str) -> str:
    return _redact_full(value)[:500]


def build_comment_body(message_id: str, content: str, created_at: str) -> str:
    """Render the PR comment for one Outbox message.

    The marker and the identifying metadata come first so that a truncated
    body still carries everything the trigger and a human reader need.
    """
    content = content or "(no content)"
    fences = re.findall(r"`+", content)
    fence = "`" * max(3, (max((len(f) for f in fences), default=0) + 1))
    header = (
        f"{OUTBOX_MARKER} outbox message `{message_id}`\n\n"
        f"- message id: `{message_id}`\n"
        f"- created at: `{created_at}`\n\n"
    )
    budget = MAX_BODY_CHARS - len(header) - 2 * len(fence) - 32
    truncated = len(content) > budget
    if truncated:
        content = content[:budget]
    body = f"{header}{fence}\n{content}\n{fence}"
    if truncated:
        body += f"\n\n_Content truncated; full message in the outbox file for `{message_id}`._"
    return body


class OutboxPRNotifier:
    """Idempotent, retrying publisher of Outbox messages to the communication PR."""

    def __init__(
        self,
        *,
        db_path: str = DEFAULT_DB_PATH,
        repository: str | None = None,
        token: str | None = None,
        transport: Transport | None = None,
        api_base: str = DEFAULT_API_BASE,
        pr_number: int | None = None,
        communication_branch: str = DEFAULT_COMMUNICATION_BRANCH,
        base_branch: str = DEFAULT_BASE_BRANCH,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.repository = repository or os.environ.get("GITHUB_REPOSITORY")
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self.api_base = api_base.rstrip("/")
        self.communication_branch = communication_branch
        self.base_branch = base_branch
        self.max_attempts = max(1, int(max_attempts))
        self.base_backoff_seconds = float(base_backoff_seconds)
        self._transport = transport
        self._clock = clock
        self._conn = init_db(db_path)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()
        self._default_pr_number = int(pr_number) if pr_number else _pr_number_from_env()

    def close(self) -> None:
        self._conn.close()

    @property
    def configured(self) -> bool:
        return bool(self.repository and (self.token or self._transport))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify(self, message_id: str, content: str, created_at: str | None = None) -> bool:
        """Record and try to deliver one Outbox message. Never raises."""
        if not message_id:
            return False
        self._enqueue(message_id, content, created_at or _utcnow_iso())
        row = self._row(message_id)
        if row is None or row["status"] != STATUS_PENDING:
            return row is not None and row["status"] == STATUS_DELIVERED
        return self._attempt(row)

    def deliver_pending(self, limit: int = 20) -> int:
        """Deliver due pending notifications; return how many were delivered."""
        rows = self._conn.execute(
            """SELECT * FROM outbox_pr_notifications
               WHERE status = ? AND next_attempt_at <= ?
               ORDER BY created_at ASC LIMIT ?""",
            (STATUS_PENDING, self._clock(), max(1, int(limit))),
        ).fetchall()
        return sum(1 for row in rows if self._attempt(row))

    def status_of(self, message_id: str) -> dict[str, Any] | None:
        row = self._row(message_id)
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    def _attempt(self, row: sqlite3.Row) -> bool:
        message_id = row["message_id"]
        if not self.configured:
            self._record_failure(row, "GITHUB_REPOSITORY and GITHUB_TOKEN/GH_TOKEN are required")
            return False
        try:
            pr_number = self._resolve_pr()
            body = build_comment_body(message_id, row["content"], row["message_created_at"])
            comment = self._request(
                "POST",
                f"/repos/{self.repository}/issues/{pr_number}/comments",
                {"body": body},
            )
        except Exception as exc:  # noqa: BLE001 - every failure is retryable
            self._record_failure(row, f"{type(exc).__name__}: {exc}")
            return False

        comment_id = comment.get("id") if isinstance(comment, dict) else None
        self._conn.execute(
            """UPDATE outbox_pr_notifications
               SET status = ?, pr_number = ?, comment_id = ?, last_error = NULL,
                   attempts = attempts + 1, updated_at = ?
               WHERE message_id = ?""",
            (STATUS_DELIVERED, pr_number, comment_id, _utcnow_iso(), message_id),
        )
        self._conn.commit()
        logger.info(
            "Outbox message %s published to communication PR #%s (comment %s)",
            message_id, pr_number, comment_id,
        )
        return True

    def _record_failure(self, row: sqlite3.Row, error: str) -> None:
        message_id = row["message_id"]
        attempts = int(row["attempts"] or 0) + 1
        terminal = attempts >= self.max_attempts
        status = STATUS_FAILED if terminal else STATUS_PENDING
        delay = self.base_backoff_seconds * (2 ** (attempts - 1))
        self._conn.execute(
            """UPDATE outbox_pr_notifications
               SET status = ?, attempts = ?, last_error = ?, next_attempt_at = ?, updated_at = ?
               WHERE message_id = ?""",
            (status, attempts, _redact(error), self._clock() + delay, _utcnow_iso(), message_id),
        )
        self._conn.commit()
        log = logger.error if terminal else logger.warning
        log(
            "Outbox message %s not published to communication PR #%s (attempt %d/%d, %s): %s",
            message_id,
            row["pr_number"] or self._active_pr_number(),
            attempts,
            self.max_attempts,
            "terminal" if terminal else "will retry",
            _redact(error),
        )

    # ------------------------------------------------------------------
    # Communication PR resolution
    # ------------------------------------------------------------------

    def _resolve_pr(self) -> int:
        """Return a usable, open communication PR number, repairing as needed."""
        pr_number = self._active_pr_number()
        try:
            pull = self._request("GET", f"/repos/{self.repository}/pulls/{pr_number}")
        except OutboxPRNotifyError as exc:
            if not _is_not_found(str(exc)):
                raise
            logger.warning("Communication PR #%s is gone: %s", pr_number, _redact(str(exc)))
            return self._recreate_channel()

        if not isinstance(pull, dict):
            raise OutboxPRNotifyError("GitHub returned an invalid pull request response")
        if pull.get("state") == "open":
            return pr_number
        if pull.get("merged"):
            logger.warning("Communication PR #%s was merged and cannot be reused", pr_number)
            return self._recreate_channel()

        head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
        branch = str(head.get("ref") or self.communication_branch)
        head_sha = str(head.get("sha") or "")
        try:
            self._ensure_branch(branch, head_sha)
            self._request(
                "PATCH", f"/repos/{self.repository}/pulls/{pr_number}", {"state": "open"}
            )
        except Exception as exc:  # noqa: BLE001 - fall back to a fresh channel
            logger.warning(
                "Reopening communication PR #%s failed: %s", pr_number, _redact(str(exc))
            )
            return self._recreate_channel()
        logger.info("Reopened communication PR #%s", pr_number)
        return pr_number

    def _ensure_branch(self, branch: str, head_sha: str) -> None:
        """Recreate the PR head branch if GitHub deleted it on close."""
        try:
            self._request("GET", f"/repos/{self.repository}/git/ref/heads/{branch}")
            return
        except OutboxPRNotifyError as exc:
            if not _is_not_found(str(exc)):
                raise
        sha = head_sha or self._base_sha()
        self._request(
            "POST",
            f"/repos/{self.repository}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": sha},
        )
        logger.warning("Recreated communication branch %s", branch)

    def _base_sha(self) -> str:
        ref = self._request("GET", f"/repos/{self.repository}/git/ref/heads/{self.base_branch}")
        sha = ((ref or {}).get("object") or {}).get("sha") if isinstance(ref, dict) else None
        if not sha:
            raise OutboxPRNotifyError(f"cannot resolve head of base branch {self.base_branch}")
        return str(sha)

    def _recreate_channel(self) -> int:
        """Last resort: build an equivalent channel and shout about the new number."""
        branch = self.communication_branch
        self._ensure_branch(branch, "")
        self._ensure_communication_file(branch)
        pull = self._request(
            "POST",
            f"/repos/{self.repository}/pulls",
            {
                "title": "Nexus communication",
                "head": branch,
                "base": self.base_branch,
                "body": (
                    "Persistent Nexus communication channel. Do not merge.\n\n"
                    f"Outbox messages are posted here as `{OUTBOX_MARKER}` comments."
                ),
            },
        )
        if not isinstance(pull, dict) or not pull.get("number"):
            raise OutboxPRNotifyError("GitHub returned an invalid pull request on recreation")
        new_number = int(pull["number"])
        self._set_active_pr_number(new_number)
        logger.error(
            "COMMUNICATION CHANNEL CHANGED: communication PR recreated as #%s (%s). "
            "The external ChatGPT Work trigger is still scoped to PR #%s and must be "
            "updated manually.",
            new_number,
            pull.get("html_url") or "",
            self._default_pr_number,
        )
        return new_number

    def _ensure_communication_file(self, branch: str) -> None:
        """Guarantee the branch differs from base, so a PR can be opened."""
        path = f"/repos/{self.repository}/contents/{COMMUNICATION_FILE}?ref={branch}"
        try:
            self._request("GET", path)
            return
        except OutboxPRNotifyError as exc:
            if not _is_not_found(str(exc)):
                raise
        content = (
            "# Nexus communication\n\n"
            "Persistent channel between Nexus and ChatGPT Work. Do not merge this PR.\n"
        )
        self._request(
            "PUT",
            f"/repos/{self.repository}/contents/{COMMUNICATION_FILE}",
            {
                "message": "chore: restore Nexus communication channel",
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "branch": branch,
            },
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_pr_notifications (
                    message_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    message_created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pr_number INTEGER,
                    comment_id INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _enqueue(self, message_id: str, content: str, created_at: str) -> None:
        now = _utcnow_iso()
        self._conn.execute(
            """INSERT OR IGNORE INTO outbox_pr_notifications
                   (message_id, content, message_created_at, status, attempts,
                    next_attempt_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, 0, 0, ?, ?)""",
            (message_id, content or "", created_at, STATUS_PENDING, now, now),
        )
        self._conn.commit()

    def _row(self, message_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM outbox_pr_notifications WHERE message_id = ?",
            (message_id,),
        ).fetchone()

    def _active_pr_number(self) -> int:
        stored = get_setting(self._conn, _PR_SETTING_KEY)
        if stored and stored.isdigit():
            return int(stored)
        return self._default_pr_number

    def _set_active_pr_number(self, pr_number: int) -> None:
        set_setting(self._conn, _PR_SETTING_KEY, str(pr_number), updated_at=_utcnow_iso())

    # ------------------------------------------------------------------
    # GitHub transport
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if self._transport is not None:
            return self._transport(method, path, payload)
        if not self.token:
            raise OutboxPRNotifyError("GitHub token is not configured")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.api_base}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "nexus-agent-lab",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise OutboxPRNotifyError(f"GitHub HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OutboxPRNotifyError(f"GitHub request failed: {exc}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OutboxPRNotifyError("GitHub returned invalid JSON") from exc


def _is_not_found(error: str) -> bool:
    return "HTTP 404" in error


def _pr_number_from_env() -> int:
    raw = (os.environ.get("NEXUS_COMMUNICATION_PR") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_PR_NUMBER


def notifier_from_env(db_path: str = DEFAULT_DB_PATH) -> OutboxPRNotifier | None:
    """Build a notifier from the environment, or ``None`` when unconfigured.

    Unconfigured is a normal local/test state, not an error: outbox publishing
    itself keeps working, only the PR notification is skipped.
    """
    notifier = OutboxPRNotifier(db_path=db_path)
    if not notifier.configured:
        notifier.close()
        return None
    return notifier
