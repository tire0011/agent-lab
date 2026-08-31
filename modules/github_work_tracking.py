from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from modules.run_store import DEFAULT_DB_PATH, _connect

logger = logging.getLogger(__name__)

_DEFAULT_API_BASE = "https://api.github.com"
_DEFAULT_TARGET_BRANCH = "main"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str, *, max_len: int = 48) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    value = value[:max_len].rstrip("-")
    return value or "work"


def _normalise_title(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def _infer_repository(repo_root: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    remote = proc.stdout.strip()
    patterns = (
        r"github\.com[:/](?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?$",
        r"api\.github\.com/repos/(?P<repo>[^/\s]+/[^/\s]+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, remote)
        if match:
            return match.group("repo").removesuffix(".git")
    return None


@dataclass(slots=True)
class GitHubWorkRef:
    issue_number: int
    issue_title: str
    branch_name: str
    source_type: str | None = None
    source_id: str | None = None
    experiment_id: str | None = None
    pr_number: int | None = None


class GitHubWorkTrackingError(RuntimeError):
    pass


Transport = Callable[[str, str, dict[str, Any] | None], Any]


class GitHubWorkTrackingService:
    """Durable GitHub lifecycle identity for Nexus implementation work."""

    def __init__(
        self,
        *,
        db_path: str = DEFAULT_DB_PATH,
        repo_root: str = "/agent",
        repository: str | None = None,
        token: str | None = None,
        api_base: str = _DEFAULT_API_BASE,
        target_branch: str = _DEFAULT_TARGET_BRANCH,
        transport: Transport | None = None,
    ) -> None:
        self.db_path = db_path
        self.repo_root = str(Path(repo_root))
        self.repository = (
            repository
            or os.environ.get("GITHUB_REPOSITORY")
            or _infer_repository(self.repo_root)
        )
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self.api_base = api_base.rstrip("/")
        self.target_branch = target_branch
        self._transport = transport
        self._conn = _connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    @property
    def configured(self) -> bool:
        return bool(self.repository and (self.token or self._transport))

    def readiness_error(self) -> str | None:
        missing: list[str] = []
        if not self.repository:
            missing.append("GITHUB_REPOSITORY")
        if not (self.token or self._transport):
            missing.append("GITHUB_TOKEN/GH_TOKEN")
        if not missing:
            return None
        missing_text = ", ".join(missing)
        return (
            "GitHub lifecycle is required for Nexus implementation work before any experiment starts. "
            f"Configure {missing_text}; issue/branch/PR operations remain blocked until then."
        )

    def require_lifecycle_ready(self) -> None:
        message = self.readiness_error()
        if message is not None:
            raise GitHubWorkTrackingError(message)

    def close(self) -> None:
        self._conn.close()

    def _ensure_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS github_work_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT,
                    source_id TEXT,
                    experiment_id TEXT UNIQUE,
                    issue_number INTEGER NOT NULL,
                    issue_title TEXT NOT NULL,
                    branch_name TEXT NOT NULL,
                    pr_number INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_github_work_source
                ON github_work_links(source_type, source_id)
                WHERE source_id IS NOT NULL
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_github_work_issue
                ON github_work_links(issue_number)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS github_experiment_links (
                    experiment_id TEXT PRIMARY KEY,
                    issue_number INTEGER NOT NULL,
                    issue_title TEXT NOT NULL,
                    branch_name TEXT NOT NULL,
                    pr_number INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def ensure_for_source(self, source) -> GitHubWorkRef:
        self.require_lifecycle_ready()

        source_type = str(getattr(source, "source_type", "task") or "task")
        source_id_raw = getattr(source, "source_id", None)
        source_id = str(source_id_raw) if source_id_raw is not None else None
        title = (getattr(source, "title", None) or "").strip()
        content = (getattr(source, "content", None) or "").strip()

        persisted = self._find_by_source(source_type, source_id) if source_id else None
        if persisted is not None:
            self._ensure_branch(persisted.branch_name)
            return persisted

        issue: dict[str, Any]
        if source_type in {"issue", "github_issue"} and source_id and source_id.isdigit():
            issue = self._request("GET", f"/repos/{self.repository}/issues/{int(source_id)}")
            if "pull_request" in issue:
                raise GitHubWorkTrackingError(f"GitHub #{source_id} is a pull request, not an Issue")
            if str(issue.get("state") or "").lower() == "closed":
                issue = self._request(
                    "PATCH",
                    f"/repos/{self.repository}/issues/{int(source_id)}",
                    {"state": "open"},
                )
        else:
            issue_title = self._issue_title(title, content)
            issue = self._find_exact_open_issue(issue_title) or self._create_issue(issue_title, content)

        issue_number = int(issue["number"])
        issue_title = str(issue.get("title") or f"Issue {issue_number}")
        branch_name = f"nexus/{issue_number}-{_slug(issue_title)}"
        ref = GitHubWorkRef(
            issue_number=issue_number,
            issue_title=issue_title,
            branch_name=branch_name,
            source_type=source_type,
            source_id=source_id,
        )
        self._persist_source(ref)
        self._ensure_branch(branch_name)
        return ref

    def bind_experiment(self, ref: GitHubWorkRef, experiment_id: str) -> GitHubWorkRef:
        if not experiment_id:
            raise ValueError("experiment_id must not be empty")
        bound = GitHubWorkRef(
            issue_number=ref.issue_number,
            issue_title=ref.issue_title,
            branch_name=ref.branch_name,
            source_type=ref.source_type,
            source_id=ref.source_id,
            experiment_id=experiment_id,
            pr_number=ref.pr_number,
        )
        if bound.source_id is not None:
            self._persist_source(bound)
        self._persist_experiment(bound)
        return bound

    def for_experiment(self, experiment_id: str) -> GitHubWorkRef | None:
        row = self._conn.execute(
            "SELECT * FROM github_experiment_links WHERE experiment_id = ? LIMIT 1",
            (experiment_id,),
        ).fetchone()
        if row is not None:
            return GitHubWorkRef(
                issue_number=int(row["issue_number"]),
                issue_title=str(row["issue_title"]),
                branch_name=str(row["branch_name"]),
                experiment_id=str(row["experiment_id"]),
                pr_number=int(row["pr_number"]) if row["pr_number"] is not None else None,
            )
        legacy = self._conn.execute(
            "SELECT * FROM github_work_links WHERE experiment_id = ? LIMIT 1",
            (experiment_id,),
        ).fetchone()
        return self._source_row_to_ref(legacy) if legacy is not None else None

    def ensure_for_experiment(self, experiment_id: str) -> GitHubWorkRef | None:
        ref = self.for_experiment(experiment_id)
        if ref is None:
            return None
        self.require_lifecycle_ready()
        self._ensure_branch(ref.branch_name)
        return ref

    def ensure_pull_request(
        self,
        ref: GitHubWorkRef,
        *,
        title: str,
        body: str,
        draft: bool = False,
    ) -> GitHubWorkRef:
        self.require_lifecycle_ready()
        owner = self.repository.split("/", 1)[0]
        query = urllib.parse.urlencode(
            {
                "state": "open",
                "head": f"{owner}:{ref.branch_name}",
                "base": self.target_branch,
                "per_page": 10,
            }
        )
        pulls = self._request("GET", f"/repos/{self.repository}/pulls?{query}")
        pr = pulls[0] if isinstance(pulls, list) and pulls else None
        if pr is None:
            pr = self._request(
                "POST",
                f"/repos/{self.repository}/pulls",
                {
                    "title": title[:240],
                    "head": ref.branch_name,
                    "base": self.target_branch,
                    "body": body[:64000],
                    "draft": bool(draft),
                },
            )
        ref.pr_number = int(pr["number"])
        if ref.source_id is not None:
            self._persist_source(ref)
        if ref.experiment_id:
            self._persist_experiment(ref)
        return ref

    def merge_and_complete(self, ref: GitHubWorkRef, *, expected_head_sha: str | None = None) -> str:
        if ref.pr_number is None:
            raise GitHubWorkTrackingError("cannot merge work without a pull request")
        payload: dict[str, Any] = {"merge_method": "squash"}
        if expected_head_sha:
            payload["sha"] = expected_head_sha
        result = self._request(
            "PUT",
            f"/repos/{self.repository}/pulls/{ref.pr_number}/merge",
            payload,
        )
        if not bool(result.get("merged")):
            raise GitHubWorkTrackingError(str(result.get("message") or "GitHub did not merge the pull request"))
        self._request(
            "PATCH",
            f"/repos/{self.repository}/issues/{ref.issue_number}",
            {"state": "closed", "state_reason": "completed"},
        )
        return str(result.get("sha") or expected_head_sha or "")

    def _issue_title(self, title: str, content: str) -> str:
        if title:
            return " ".join(title.split())[:240]
        first_line = next((line.strip() for line in content.splitlines() if line.strip()), "Nexus work item")
        return " ".join(first_line.split())[:240]

    def _find_exact_open_issue(self, title: str) -> dict[str, Any] | None:
        query = urllib.parse.urlencode(
            {
                "q": f'repo:{self.repository} is:issue is:open in:title "{title[:120]}"',
                "per_page": 10,
            }
        )
        result = self._request("GET", f"/search/issues?{query}")
        wanted = _normalise_title(title)
        matches = [
            item
            for item in (result.get("items") or [])
            if _normalise_title(str(item.get("title") or "")) == wanted
        ]
        return matches[0] if len(matches) == 1 else None

    def _create_issue(self, title: str, content: str) -> dict[str, Any]:
        body = (
            "Created automatically by Nexus before implementation so the work is durably traceable in GitHub.\n\n"
            "## Nexus task\n\n"
            f"{content[:30000]}"
        )
        return self._request(
            "POST",
            f"/repos/{self.repository}/issues",
            {"title": title, "body": body},
        )

    def _ensure_branch(self, branch_name: str) -> None:
        quoted = urllib.parse.quote(branch_name, safe="")
        try:
            self._request("GET", f"/repos/{self.repository}/git/ref/heads/{quoted}")
            return
        except GitHubWorkTrackingError as exc:
            if "HTTP 404" not in str(exc):
                raise
        base = self._request(
            "GET",
            f"/repos/{self.repository}/git/ref/heads/{urllib.parse.quote(self.target_branch, safe='')}",
        )
        sha = str(((base.get("object") or {}).get("sha") or "")).strip()
        if not sha:
            raise GitHubWorkTrackingError("could not resolve target branch SHA")
        self._request(
            "POST",
            f"/repos/{self.repository}/git/refs",
            {"ref": f"refs/heads/{branch_name}", "sha": sha},
        )

    def _find_by_source(self, source_type: str, source_id: str) -> GitHubWorkRef | None:
        row = self._conn.execute(
            "SELECT * FROM github_work_links WHERE source_type = ? AND source_id = ? ORDER BY id LIMIT 1",
            (source_type, source_id),
        ).fetchone()
        return self._source_row_to_ref(row) if row is not None else None

    def _persist_source(self, ref: GitHubWorkRef) -> None:
        if ref.source_id is None:
            return
        now = _utcnow_iso()
        row = self._conn.execute(
            "SELECT id FROM github_work_links WHERE source_type = ? AND source_id = ?",
            (ref.source_type, ref.source_id),
        ).fetchone()
        with self._conn:
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO github_work_links (
                        source_type, source_id, experiment_id, issue_number,
                        issue_title, branch_name, pr_number, created_at, updated_at
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ref.source_type,
                        ref.source_id,
                        ref.issue_number,
                        ref.issue_title,
                        ref.branch_name,
                        ref.pr_number,
                        now,
                        now,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE github_work_links
                    SET issue_number = ?, issue_title = ?, branch_name = ?,
                        pr_number = COALESCE(?, pr_number), updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        ref.issue_number,
                        ref.issue_title,
                        ref.branch_name,
                        ref.pr_number,
                        now,
                        int(row["id"]),
                    ),
                )

    def _persist_experiment(self, ref: GitHubWorkRef) -> None:
        if not ref.experiment_id:
            return
        now = _utcnow_iso()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO github_experiment_links (
                    experiment_id, issue_number, issue_title, branch_name,
                    pr_number, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(experiment_id) DO UPDATE SET
                    issue_number=excluded.issue_number,
                    issue_title=excluded.issue_title,
                    branch_name=excluded.branch_name,
                    pr_number=COALESCE(excluded.pr_number, github_experiment_links.pr_number),
                    updated_at=excluded.updated_at
                """,
                (
                    ref.experiment_id,
                    ref.issue_number,
                    ref.issue_title,
                    ref.branch_name,
                    ref.pr_number,
                    now,
                    now,
                ),
            )

    @staticmethod
    def _source_row_to_ref(row: sqlite3.Row) -> GitHubWorkRef:
        return GitHubWorkRef(
            issue_number=int(row["issue_number"]),
            issue_title=str(row["issue_title"]),
            branch_name=str(row["branch_name"]),
            source_type=row["source_type"],
            source_id=row["source_id"],
            experiment_id=row["experiment_id"],
            pr_number=int(row["pr_number"]) if row["pr_number"] is not None else None,
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if self._transport is not None:
            return self._transport(method, path, payload)
        if not self.token:
            raise GitHubWorkTrackingError("GitHub token is not configured")
        url = f"{self.api_base}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
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
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise GitHubWorkTrackingError(f"GitHub HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GitHubWorkTrackingError(f"GitHub request failed: {exc}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GitHubWorkTrackingError("GitHub returned invalid JSON") from exc
