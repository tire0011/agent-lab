from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from modules.run_store import DEFAULT_DB_PATH, _connect
from modules.work_directives import WorkDirectives, parse_work_directives

logger = logging.getLogger(__name__)

_DEFAULT_API_BASE = "https://api.github.com"
_DEFAULT_TARGET_BRANCH = "main"
_MAX_ISSUE_BODY_CHARS = 16_000
_DEFAULT_CI_WORKFLOW = "tests.yml"
_DEFAULT_CI_START_TIMEOUT_SECONDS = 90.0
_DEFAULT_CI_COMPLETION_TIMEOUT_SECONDS = 1500.0
_DEFAULT_CI_POLL_INTERVAL_SECONDS = 10.0
_MAX_COMMIT_TITLE_CHARS = 72

# The opening sentence of every lifecycle-readiness refusal. Exported so a
# consumer that has only a *persisted* refusal text - written before the
# blocker was recorded in a machine-readable column (#171) - can still
# recognize it as this one condition instead of guessing at keywords. It
# survives redaction (it names no secret and no path), unlike the
# "Configure GITHUB_TOKEN/..." sentence that follows it.
LIFECYCLE_READINESS_MESSAGE = (
    "GitHub lifecycle is required for Nexus implementation work."
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str, *, max_len: int = 48) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    value = value[:max_len].rstrip("-")
    return value or "work"


def _normalise_title(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def _issue_commit_title(issue_title: str, issue_number: int, *, max_len: int = _MAX_COMMIT_TITLE_CHARS) -> str:
    """Return ``<issue title> (#N)`` without a ``WIP:`` prefix, at most ``max_len`` chars (#423).

    The planner reads main's ``git log`` to see which issues already landed;
    a squash title naming only the experiment id cannot be linked back.
    """
    suffix = f" (#{issue_number})"
    title = " ".join((issue_title or "").split())
    while re.match(r"(?i)^wip:\s*", title):
        title = re.sub(r"(?i)^wip:\s*", "", title, count=1)
    title = title or f"Issue {issue_number}"
    room = max(max_len - len(suffix), 1)
    if len(title) > room:
        title = title[:room].rstrip()
    return title + suffix


def _experiment_trailer(experiment_id: str | None) -> str:
    return f"Nexus-Experiment: {experiment_id}" if experiment_id else ""


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
    checkpoint_sha: str | None = None


@dataclass(frozen=True, slots=True)
class GitHubCIVerification:
    status: str
    commit_sha: str
    conclusion: str | None = None
    run_id: int | None = None
    url: str | None = None
    reason: str | None = None


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
        return (
            f"{LIFECYCLE_READINESS_MESSAGE} "
            f"Configure {', '.join(missing)} before starting or resuming implementation."
        )

    def require_lifecycle_ready(self) -> None:
        message = self.readiness_error()
        if message:
            raise GitHubWorkTrackingError(message)

    def get_issue(self, issue_number: int) -> dict[str, Any]:
        """Fetch one Issue without mutating GitHub or exposing credentials."""
        if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number < 1:
            raise ValueError("issue_number must be a positive integer")
        self.require_lifecycle_ready()
        issue = self._request(
            "GET",
            f"/repos/{self.repository}/issues/{issue_number}",
        )
        if not isinstance(issue, dict):
            raise GitHubWorkTrackingError("GitHub returned an invalid Issue response")
        if "pull_request" in issue:
            raise GitHubWorkTrackingError(
                f"GitHub #{issue_number} is a pull request, not an Issue"
            )

        body = str(issue.get("body") or "")
        labels = issue.get("labels") if isinstance(issue.get("labels"), list) else []
        assignees = issue.get("assignees") if isinstance(issue.get("assignees"), list) else []
        user = issue.get("user") if isinstance(issue.get("user"), dict) else {}
        return {
            "number": int(issue.get("number") or issue_number),
            "title": str(issue.get("title") or ""),
            "body": body[:_MAX_ISSUE_BODY_CHARS],
            "body_truncated": len(body) > _MAX_ISSUE_BODY_CHARS,
            "state": str(issue.get("state") or ""),
            "url": str(issue.get("html_url") or issue.get("url") or ""),
            "author": str(user.get("login") or ""),
            "labels": [
                str(label.get("name") if isinstance(label, dict) else label)
                for label in labels[:20]
            ],
            "assignees": [
                str(assignee.get("login") if isinstance(assignee, dict) else assignee)
                for assignee in assignees[:20]
            ],
            "created_at": str(issue.get("created_at") or ""),
            "updated_at": str(issue.get("updated_at") or ""),
        }

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
                    checkpoint_sha TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(github_experiment_links)")
            }
            if "checkpoint_sha" not in columns:
                self._conn.execute(
                    "ALTER TABLE github_experiment_links ADD COLUMN checkpoint_sha TEXT"
                )
            # Durable record of authoritative CI verdicts (Issue #138).
            # Once Nexus has accepted a completed GitHub Actions conclusion
            # for an exact commit, that is a lifecycle fact. Re-deriving it
            # needs live credentials, so a restart into a context without a
            # token made already-accepted evidence disappear and blocked
            # the #136 retry. Keyed by commit SHA because that is what the
            # evidence is *about*; a verdict is never reused for any other
            # commit. No token or secret is stored here.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS github_ci_verifications (
                    commit_sha TEXT PRIMARY KEY,
                    experiment_id TEXT,
                    status TEXT NOT NULL,
                    conclusion TEXT,
                    run_id INTEGER,
                    url TEXT,
                    workflow TEXT,
                    provider TEXT NOT NULL DEFAULT 'github_actions',
                    base_sha TEXT,
                    observed_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_github_ci_verifications_experiment
                ON github_ci_verifications(experiment_id, observed_at)
                """
            )
            ci_columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(github_ci_verifications)")
            }
            if "base_sha" not in ci_columns:
                self._conn.execute(
                    "ALTER TABLE github_ci_verifications ADD COLUMN base_sha TEXT"
                )

    def ensure_for_source(self, source) -> GitHubWorkRef:
        self.require_lifecycle_ready()

        source_type = str(getattr(source, "source_type", "task") or "task")
        source_id_raw = getattr(source, "source_id", None)
        source_id = str(source_id_raw) if source_id_raw is not None else None
        title = (getattr(source, "title", None) or "").strip()
        content = (getattr(source, "content", None) or "").strip()

        directives = parse_work_directives(f"{title}\n{content}")

        persisted = self._find_by_source(source_type, source_id) if source_id else None
        if persisted is not None:
            # A link from an earlier run is no licence to re-create a branch
            # that has since been deleted: the task's own constraint decides.
            self._require_reusable_branch(persisted.branch_name, directives)
            self._ensure_branch(persisted.branch_name)
            return persisted

        issue: dict[str, Any]
        if source_type in {"issue", "github_issue"} and source_id and source_id.isdigit():
            issue = self._reuse_issue(int(source_id))
        elif directives.issue_number is not None:
            # Issue #184: the task names the Issue to work on, so that Issue
            # *is* the work-tracking record. Opening a second one here is how
            # #181 was created alongside #180.
            issue = self._reuse_requested_issue(directives.issue_number)
        elif directives.forbid_new_issue:
            # Explicitly told not to create one, and no single Issue to reuse:
            # stop and say so instead of silently creating a replacement.
            raise GitHubWorkTrackingError(self._unusable_reuse_message(directives))
        else:
            issue_title = self._issue_title(title, content)
            issue = self._find_exact_open_issue(issue_title) or self._create_issue(issue_title, content)

        issue_number = int(issue["number"])
        issue_title = str(issue.get("title") or f"Issue {issue_number}")
        branch_name = directives.branch_name or f"nexus/{issue_number}-{_slug(issue_title)}"
        # Checked before the link is persisted, so a refused task leaves no
        # record pointing at a branch it was never allowed to create.
        self._require_reusable_branch(branch_name, directives)
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

    def _require_reusable_branch(self, branch_name: str, directives: WorkDirectives) -> None:
        """Refuse to *create* a branch the task said to reuse or not to add.

        Issue #184: honouring "use branch X" by creating X when it is missing
        is the same silent substitution as opening a duplicate Issue - the
        task ends up on a branch nobody asked for. Branch reuse fails closed.
        """
        if directives.branch_name is None and not directives.forbid_new_branch:
            return
        if self._branch_exists(branch_name):
            return
        if directives.branch_name is not None:
            raise GitHubWorkTrackingError(
                f"task requires reusing branch {branch_name!r}, but it does not "
                f"exist in {self.repository}; push that branch or drop the reuse "
                "instruction"
            )
        raise GitHubWorkTrackingError(
            "task forbids creating a new branch but names none to reuse, and "
            f"{branch_name!r} does not exist; name the existing branch to reuse"
        )

    def _branch_exists(self, branch_name: str) -> bool:
        quoted = urllib.parse.quote(branch_name, safe="")
        try:
            self._request("GET", f"/repos/{self.repository}/git/ref/heads/{quoted}")
        except GitHubWorkTrackingError as exc:
            if "HTTP 404" in str(exc):
                return False
            raise
        return True

    def _reuse_issue(self, issue_number: int) -> dict[str, Any]:
        """Adopt an existing Issue as the work record, reopening it if closed."""
        issue = self._request("GET", f"/repos/{self.repository}/issues/{issue_number}")
        if not isinstance(issue, dict) or not issue.get("number"):
            raise GitHubWorkTrackingError(
                f"GitHub #{issue_number} did not return a usable Issue"
            )
        if "pull_request" in issue:
            raise GitHubWorkTrackingError(
                f"GitHub #{issue_number} is a pull request, not an Issue"
            )
        if str(issue.get("state") or "").lower() == "closed":
            issue = self._request(
                "PATCH",
                f"/repos/{self.repository}/issues/{issue_number}",
                {"state": "open"},
            )
        return issue

    def _reuse_requested_issue(self, issue_number: int) -> dict[str, Any]:
        try:
            return self._reuse_issue(issue_number)
        except GitHubWorkTrackingError as exc:
            raise GitHubWorkTrackingError(
                f"task requires reusing GitHub issue #{issue_number}, but it cannot "
                f"be used: {exc}"
            ) from exc

    @staticmethod
    def _unusable_reuse_message(directives: WorkDirectives) -> str:
        mentioned = directives.mentioned_issue_numbers
        if len(mentioned) > 1:
            candidates = ", ".join(f"#{number}" for number in mentioned)
            return (
                "task forbids creating a new tracking issue but names more than one "
                f"candidate ({candidates}); name the single issue to reuse"
            )
        return (
            "task forbids creating a new tracking issue but names no issue to reuse; "
            "reference the existing issue (for example 'implement issue #180')"
        )

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
            checkpoint_sha=ref.checkpoint_sha,
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
                checkpoint_sha=str(row["checkpoint_sha"]) if row["checkpoint_sha"] else None,
            )
        legacy = self._conn.execute(
            "SELECT * FROM github_work_links WHERE experiment_id = ? LIMIT 1",
            (experiment_id,),
        ).fetchone()
        return self._source_row_to_ref(legacy) if legacy is not None else None

    def ensure_for_experiment(self, experiment_id: str) -> GitHubWorkRef:
        self.require_lifecycle_ready()
        ref = self.for_experiment(experiment_id)
        if ref is None:
            raise GitHubWorkTrackingError(
                f"experiment {experiment_id} has no persisted GitHub Issue/branch link"
            )
        self._ensure_branch(ref.branch_name)
        return ref

    def recover_experiment(self, experiment) -> GitHubWorkRef:
        """Bind a legacy experiment without allocating a new worktree."""
        self.require_lifecycle_ready()
        existing = self.for_experiment(str(experiment.id))
        if existing is not None:
            self._ensure_branch(existing.branch_name)
            return existing
        source = type(
            "RecoveredExperimentSource",
            (),
            {
                "source_type": "experiment_recovery",
                "source_id": str(experiment.id),
                "title": f"Recover Nexus experiment {str(experiment.id)[:8]}",
                "content": str(experiment.goal),
            },
        )()
        return self.bind_experiment(self.ensure_for_source(source), str(experiment.id))

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

    def checkpoint_experiment(self, experiment_id: str, worktree_path: str) -> GitHubWorkRef:
        """Publish intended worktree changes without changing its HEAD or index."""
        ref = self.ensure_for_experiment(experiment_id)
        status = self._git(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=worktree_path,
        ).stdout
        changed_paths = self._status_paths(status)
        publishable = [path for path in changed_paths if self._publishable_path(path)]
        if not publishable:
            excluded = sorted(set(changed_paths) - set(publishable))
            detail = f"; excluded runtime paths: {', '.join(excluded[:10])}" if excluded else ""
            raise GitHubWorkTrackingError(
                "implementation produced no publishable source/test/documentation changes" + detail
            )

        remote_sha = self._remote_branch_sha(ref.branch_name)
        worktree_head = self._git(["rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()
        checkpoint_base = remote_sha
        if ref.checkpoint_sha != remote_sha:
            remote_descends_from_worktree = self._git(
                ["merge-base", "--is-ancestor", worktree_head, remote_sha],
                cwd=worktree_path,
                check=False,
            )
            if remote_descends_from_worktree.returncode != 0:
                worktree_descends_from_remote = self._git(
                    ["merge-base", "--is-ancestor", remote_sha, worktree_head],
                    cwd=worktree_path,
                    check=False,
                )
                if worktree_descends_from_remote.returncode == 0:
                    checkpoint_base = worktree_head
                else:
                    raise GitHubWorkTrackingError(
                        "Issue branch no longer descends from the experiment worktree; manual reconciliation required"
                    )

            if checkpoint_base == remote_sha:
                upstream_paths = set(
                    self._git(
                        ["diff", "--name-only", f"{worktree_head}..{remote_sha}"],
                        cwd=worktree_path,
                    ).stdout.splitlines()
                )
                overlap = sorted(upstream_paths.intersection(publishable))
                if overlap:
                    raise GitHubWorkTrackingError(
                        "Issue branch changed the same files as the worktree: " + ", ".join(overlap[:20])
                    )

        with tempfile.TemporaryDirectory(prefix="nexus-checkpoint-index-") as temp_dir:
            env = dict(os.environ)
            env["GIT_INDEX_FILE"] = str(Path(temp_dir) / "index")
            self._git(["read-tree", checkpoint_base], cwd=worktree_path, env=env)
            self._git(["add", "-A", "--", *publishable], cwd=worktree_path, env=env)
            tree_sha = self._git(["write-tree"], cwd=worktree_path, env=env).stdout.strip()
        base_tree = self._git(
            ["rev-parse", f"{checkpoint_base}^{{tree}}"], cwd=worktree_path
        ).stdout.strip()
        checkpoint_sha = checkpoint_base
        if tree_sha != base_tree:
            if ref.issue_number:
                checkpoint_message = (
                    f"WIP: {_issue_commit_title(ref.issue_title, ref.issue_number)}\n\n"
                    f"{_experiment_trailer(experiment_id)}"
                )
            else:
                checkpoint_message = f"WIP: Nexus experiment {experiment_id[:8]}"
            checkpoint_sha = self._git(
                [
                    "commit-tree",
                    tree_sha,
                    "-p",
                    checkpoint_base,
                    "-m",
                    checkpoint_message,
                ],
                cwd=worktree_path,
            ).stdout.strip()
            self._git(
                ["push", "origin", f"{checkpoint_sha}:refs/heads/{ref.branch_name}"],
                cwd=worktree_path,
            )
        ref.checkpoint_sha = checkpoint_sha
        self._persist_experiment(ref)
        ref = self.ensure_pull_request(
            ref,
            title=f"WIP: {ref.issue_title}",
            body=(
                f"Tracks #{ref.issue_number}\n\n"
                f"Nexus experiment: `{experiment_id}`\n\n"
                "Implementation checkpoint published. Verification is pending.\n\n"
                f"Changed files: {', '.join(publishable[:30])}"
            ),
            draft=True,
        )
        return ref

    def experiment_base_sha(self, experiment_id: str) -> str:
        """Fetch and return the issue branch commit an experiment must continue.

        A later experiment for the same issue must inherit work already
        checkpointed by an earlier experiment.  Fetching by branch also makes
        the returned GitHub SHA available in a fresh deployment checkout's
        object database without creating or moving a local tracking branch.
        """
        ref = self.ensure_for_experiment(experiment_id)
        remote_sha = self._remote_branch_sha(ref.branch_name)
        self._git(
            ["fetch", "--no-tags", "origin", f"refs/heads/{ref.branch_name}"],
            cwd=self.repo_root,
        )
        self._git(
            ["cat-file", "-e", f"{remote_sha}^{{commit}}"],
            cwd=self.repo_root,
        )
        return remote_sha

    def record_verification(
        self,
        experiment_id: str,
        *,
        status: str,
        command: list[str] | None,
        exit_code: int | None,
        error: str | None,
        output: str | None,
    ) -> None:
        ref = self.ensure_for_experiment(experiment_id)
        if ref.pr_number is None:
            return
        command_text = " ".join(command or []) or "(not run)"
        body = (
            f"Tracks #{ref.issue_number}\n\n"
            f"Nexus experiment: `{experiment_id}`\n\n"
            f"Verification status: `{status}`\n\n"
            f"Command: `{command_text[:1000]}`\n\n"
            f"Exit code: `{exit_code if exit_code is not None else 'none'}`\n\n"
            f"Error: {(error or 'none')[:2000]}\n\n"
            "```text\n"
            f"{(output or '')[-6000:]}\n"
            "```"
        )
        self._request(
            "PATCH",
            f"/repos/{self.repository}/pulls/{ref.pr_number}",
            {"body": body[:64000]},
        )

    # Only a completed run carries a verdict worth keeping; "unavailable"
    # says nothing about the commit and must never be cached, or a
    # temporary outage would masquerade as evidence.
    _AUTHORITATIVE_CI_STATUSES = ("passed", "failed")

    def record_ci_verification(
        self,
        verification: GitHubCIVerification,
        *,
        experiment_id: str | None = None,
        workflow: str = _DEFAULT_CI_WORKFLOW,
        base_sha: str | None = None,
    ) -> bool:
        """Persist an authoritative CI verdict for its exact commit (#138).

        ``base_sha`` is the target-branch commit the verified checkpoint
        was built on. It is stored because a verdict is only authoritative
        for adoption while the integration base is unchanged (#134): the
        same checkpoint cherry-picked onto a moved base produces a tree CI
        never saw. Without it a reader cannot tell the two apart.

        Returns whether anything was stored. The most recent authoritative
        observation for a commit wins: a re-run that flips the conclusion
        overwrites the previous record, which is what makes the precedence
        deterministic without a live lookup at read time.
        """
        commit_sha = (verification.commit_sha or "").strip()
        if not commit_sha or verification.status not in self._AUTHORITATIVE_CI_STATUSES:
            return False
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO github_ci_verifications (
                    commit_sha, experiment_id, status, conclusion, run_id, url,
                    workflow, provider, base_sha, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'github_actions', ?, ?)
                ON CONFLICT(commit_sha) DO UPDATE SET
                    experiment_id=COALESCE(excluded.experiment_id, github_ci_verifications.experiment_id),
                    status=excluded.status,
                    conclusion=excluded.conclusion,
                    run_id=excluded.run_id,
                    url=excluded.url,
                    workflow=excluded.workflow,
                    provider=excluded.provider,
                    base_sha=excluded.base_sha,
                    observed_at=excluded.observed_at
                """,
                (
                    commit_sha,
                    experiment_id,
                    verification.status,
                    verification.conclusion,
                    verification.run_id,
                    verification.url,
                    workflow,
                    (base_sha or "").strip() or None,
                    _utcnow_iso(),
                ),
            )
        return True

    def recorded_ci_details(self, commit_sha: str) -> dict[str, Any] | None:
        """The stored row for this commit as plain fields, ignoring base validity.

        Separate from :meth:`recorded_ci_for` on purpose: that method
        answers "may this verdict be used", and deliberately returns
        ``None`` both when nothing was ever recorded and when what was
        recorded no longer applies to the current base. Diagnosis needs to
        tell those apart (Issue #141), and needs the row's own base and
        timestamp to say *why*. This never decides anything - callers must
        still go through ``recorded_ci_for`` for the verdict itself.

        The table holds no credentials, so every field here is safe to log.
        """
        sha = (commit_sha or "").strip()
        if not sha:
            return None
        row = self._conn.execute(
            "SELECT * FROM github_ci_verifications WHERE commit_sha = ? LIMIT 1", (sha,)
        ).fetchone()
        if row is None:
            return None
        return {
            "commit_sha": str(row["commit_sha"]),
            "experiment_id": str(row["experiment_id"]) if row["experiment_id"] else None,
            "status": str(row["status"]),
            "conclusion": str(row["conclusion"]) if row["conclusion"] else None,
            "run_id": int(row["run_id"]) if row["run_id"] is not None else None,
            "url": str(row["url"]) if row["url"] else None,
            "workflow": str(row["workflow"]) if row["workflow"] else None,
            "provider": str(row["provider"]),
            "base_sha": str(row["base_sha"]) if row["base_sha"] else None,
            "observed_at": str(row["observed_at"]),
        }

    def recorded_ci_for(
        self, commit_sha: str, *, base_sha: str | None = None
    ) -> GitHubCIVerification | None:
        """The stored verdict for exactly this commit, or ``None``.

        A pure local read: it works after a restart and without any
        GitHub credentials, which is the whole point of the store. The
        exact-match lookup is also what enforces "a success for another
        commit is never reused" - there is simply no row to find.

        ``base_sha`` is the integration base the caller is about to use.
        When given, a verdict is only returned if it was recorded against
        that same base, which carries #134's rule into the durable store:
        a checkpoint verified on top of ``A`` says nothing about the tree
        produced by cherry-picking it onto ``B``. A row with no recorded
        base predates this and cannot be shown to satisfy the rule, so it
        is not reused either - the caller falls back to live verification.
        """
        sha = (commit_sha or "").strip()
        if not sha:
            return None
        row = self._conn.execute(
            "SELECT * FROM github_ci_verifications WHERE commit_sha = ? LIMIT 1", (sha,)
        ).fetchone()
        if row is None:
            return None
        recorded_base = str(row["base_sha"] or "").strip()
        if base_sha is not None and recorded_base != (base_sha or "").strip():
            logger.info(
                "ignoring persisted CI verification for %s: integration base moved "
                "(verified on %s, integrating onto %s)",
                sha,
                recorded_base or "unknown",
                base_sha,
            )
            return None
        return GitHubCIVerification(
            status=str(row["status"]),
            commit_sha=str(row["commit_sha"]),
            conclusion=str(row["conclusion"]) if row["conclusion"] else None,
            run_id=int(row["run_id"]) if row["run_id"] is not None else None,
            url=str(row["url"]) if row["url"] else None,
            reason=(
                f"persisted {row['provider']} verification observed at {row['observed_at']}"
            ),
        )

    def wait_for_ci(
        self,
        experiment_id: str,
        *,
        workflow: str = _DEFAULT_CI_WORKFLOW,
        start_timeout_seconds: float = _DEFAULT_CI_START_TIMEOUT_SECONDS,
        completion_timeout_seconds: float = _DEFAULT_CI_COMPLETION_TIMEOUT_SECONDS,
        poll_interval_seconds: float = _DEFAULT_CI_POLL_INTERVAL_SECONDS,
    ) -> GitHubCIVerification:
        """Return authoritative CI for the exact checkpoint commit SHA.

        Whether GitHub Actions verified this exact SHA is the only
        question this method answers (#210): it does not require the
        checkpoint's direct parent to match the current target branch, and
        it accepts both `push` and `pull_request` runs as authoritative.
        Base/target compatibility is a separate concern for the
        integration/adoption step, not a precondition for recognizing CI
        here.
        """
        try:
            ref = self.ensure_for_experiment(experiment_id)
        except GitHubWorkTrackingError as exc:
            return GitHubCIVerification(
                status="unavailable",
                commit_sha="",
                reason=f"GitHub CI identity unavailable: {str(exc)[:1000]}",
            )

        sha = (ref.checkpoint_sha or "").strip()
        if not sha:
            return GitHubCIVerification(
                status="unavailable",
                commit_sha="",
                reason="experiment checkpoint SHA is missing",
            )

        # The base persisted alongside a CI verdict must be the value
        # adoption will later compare against - the target branch's own
        # head SHA - not the checkpoint commit's direct git parent. A
        # repair checkpoint's parent is the previous checkpoint commit,
        # not `main` (#210); persisting that as `base_sha` would make
        # `recorded_ci_for(checkpoint_sha, base_sha=integration_base)`
        # reject a perfectly reusable verdict after every restart, even
        # when `main` never moved. Recognizing checkpoint CI itself must
        # never depend on this lookup succeeding or matching anything, so
        # a failure here only means the verdict is persisted without a
        # base (and so is not reused from the store later) - it never
        # blocks the live CI check below.
        integration_base = ""
        try:
            target = self._request(
                "GET",
                f"/repos/{self.repository}/git/ref/heads/{urllib.parse.quote(self.target_branch, safe='')}",
            )
            integration_base = str(((target.get("object") or {}).get("sha") or "")).strip()
        except GitHubWorkTrackingError:
            logger.warning(
                "could not resolve current target-branch head while verifying %s; "
                "verdict will be persisted without a base",
                sha,
                exc_info=True,
            )

        started_deadline = time.monotonic() + max(0.0, start_timeout_seconds)
        completion_deadline: float | None = None
        encoded_workflow = urllib.parse.quote(workflow, safe="")
        query = urllib.parse.urlencode({"head_sha": sha, "per_page": 20})
        path = (
            f"/repos/{self.repository}/actions/workflows/{encoded_workflow}/runs?{query}"
        )

        while True:
            try:
                response = self._request("GET", path)
            except GitHubWorkTrackingError as exc:
                return GitHubCIVerification(
                    status="unavailable",
                    commit_sha=sha,
                    reason=f"GitHub Actions lookup failed: {str(exc)[:1000]}",
                )

            # Authoritative CI for the exact checkpoint SHA is accepted
            # regardless of what triggered the run: a `pull_request` run
            # (the common case once the checkpoint lives on a PR branch,
            # #210) is just as authoritative as a `push` run.
            runs = [
                run
                for run in (response.get("workflow_runs") or [])
                if str(run.get("head_sha") or "") == sha
                and str(run.get("event") or "") in ("push", "pull_request")
            ]
            runs.sort(
                key=lambda run: (int(run.get("run_attempt") or 0), int(run.get("id") or 0)),
                reverse=True,
            )
            run = runs[0] if runs else None
            now = time.monotonic()
            if run is not None:
                if completion_deadline is None:
                    completion_deadline = now + max(0.0, completion_timeout_seconds)
                if str(run.get("status") or "").lower() == "completed":
                    conclusion = str(run.get("conclusion") or "unknown").lower()
                    run_sha = str(run.get("head_sha") or "").strip()
                    if conclusion == "success":
                        status = "passed"
                        reason = None
                    elif conclusion == "failure":
                        status = "failed"
                        reason = None
                    else:
                        status = "unavailable"
                        reason = f"GitHub Actions completed without authoritative result: {conclusion}"
                    verification = GitHubCIVerification(
                        status=status,
                        commit_sha=run_sha,
                        conclusion=conclusion,
                        run_id=int(run["id"]) if run.get("id") is not None else None,
                        url=str(run.get("html_url") or "") or None,
                        reason=reason,
                    )
                    # Persist here, at the single point that knows the
                    # verdict is authoritative and which commit it is for,
                    # so every caller populates the store - including the
                    # verification path, which is where the evidence in
                    # #138's reproduction was first seen and then lost.
                    # Only a verdict for the SHA we actually asked about is
                    # kept; a run reported for some other commit is not
                    # evidence for this one.
                    if run_sha == sha:
                        try:
                            # `integration_base` is the target branch's own
                            # head SHA at verification time (best-effort;
                            # empty when it could not be resolved). Storing
                            # it - not the checkpoint's git parent - is what
                            # lets a later, credential-less reader decide
                            # whether this verdict still applies to the
                            # base it is about to integrate onto.
                            self.record_ci_verification(
                                verification,
                                experiment_id=experiment_id,
                                workflow=workflow,
                                base_sha=integration_base,
                            )
                        except sqlite3.Error:
                            logger.warning(
                                "could not persist CI verification for %s", sha, exc_info=True
                            )
                    return verification
                if now >= completion_deadline:
                    return GitHubCIVerification(
                        status="unavailable",
                        commit_sha=sha,
                        run_id=int(run["id"]) if run.get("id") is not None else None,
                        url=str(run.get("html_url") or "") or None,
                        reason="matching GitHub Actions run did not complete within the bounded wait",
                    )
            elif now >= started_deadline:
                return GitHubCIVerification(
                    status="unavailable",
                    commit_sha=sha,
                    reason="no matching GitHub Actions run started for the checkpoint SHA",
                )

            time.sleep(max(0.01, poll_interval_seconds))

    def publish_final_tree(
        self,
        ref: GitHubWorkRef,
        *,
        integrated_commit_sha: str,
        worktree_path: str,
        experiment_id: str,
    ) -> str:
        """Append the verified integration tree to the existing checkpoint history."""
        self.require_lifecycle_ready()
        remote_sha = self._remote_branch_sha(ref.branch_name)
        final_tree = self._git(
            ["rev-parse", f"{integrated_commit_sha}^{{tree}}"], cwd=worktree_path
        ).stdout.strip()
        remote_tree = self._git(
            ["rev-parse", f"{remote_sha}^{{tree}}"], cwd=worktree_path
        ).stdout.strip()
        final_sha = remote_sha
        if final_tree != remote_tree:
            final_sha = self._git(
                [
                    "commit-tree",
                    final_tree,
                    "-p",
                    remote_sha,
                    "-m",
                    f"Finalize Nexus experiment {experiment_id[:8]}",
                ],
                cwd=worktree_path,
            ).stdout.strip()
            self._git(
                ["push", "origin", f"{final_sha}:refs/heads/{ref.branch_name}"],
                cwd=worktree_path,
            )
        ref.checkpoint_sha = final_sha
        self._persist_experiment(ref)
        return final_sha

    def mark_pull_request_ready(self, ref: GitHubWorkRef) -> None:
        if ref.pr_number is None:
            raise GitHubWorkTrackingError("cannot mark missing pull request ready")
        pull = self._request(
            "GET", f"/repos/{self.repository}/pulls/{ref.pr_number}"
        )
        if not bool(pull.get("draft")):
            return
        node_id = str(pull.get("node_id") or "")
        if not node_id:
            raise GitHubWorkTrackingError("GitHub pull request node id is missing")
        self._request(
            "POST",
            "/graphql",
            {
                "query": (
                    "mutation($id:ID!){markPullRequestReadyForReview(" 
                    "input:{pullRequestId:$id}){pullRequest{number isDraft}}}"
                ),
                "variables": {"id": node_id},
            },
        )

    def merged_commit_sha(self, ref: GitHubWorkRef) -> str | None:
        """The merge commit of *ref*'s pull request, or ``None`` if unmerged.

        GitHub itself is the durable record of whether a merge happened, so
        this is the evidence a restarted adoption uses to tell "the merge
        never ran" apart from "the merge succeeded and the process died
        before it could persist that". Returns ``None`` when there is no
        pull request yet or it is not merged; a lookup failure propagates
        as ``GitHubWorkTrackingError`` rather than being reported as "not
        merged", since guessing here could cause a duplicate merge.
        """
        if ref.pr_number is None:
            return None
        pull = self._request("GET", f"/repos/{self.repository}/pulls/{ref.pr_number}")
        if not bool(pull.get("merged")):
            return None
        return str(pull.get("merge_commit_sha") or "") or None

    def merge_and_complete(self, ref: GitHubWorkRef, *, expected_head_sha: str | None = None) -> str:
        if ref.pr_number is None:
            raise GitHubWorkTrackingError("cannot merge work without a pull request")

        # A pull request that GitHub already reports as merged must never be
        # merged a second time: after a restart mid-adoption the merge may
        # already have happened, and re-issuing it would either fail the
        # whole adoption or (worse) land the change twice.
        already_merged = self.merged_commit_sha(ref)
        if already_merged is not None:
            self._close_completed_issue(ref)
            return already_merged

        payload: dict[str, Any] = {"merge_method": "squash"}
        if ref.issue_number:
            # Without an explicit title GitHub reuses a single commit's
            # message, so main would only show the experiment id (#423).
            payload["commit_title"] = _issue_commit_title(ref.issue_title, ref.issue_number)
            payload["commit_message"] = "\n\n".join(
                part
                for part in (f"Closes #{ref.issue_number}", _experiment_trailer(ref.experiment_id))
                if part
            )
        if expected_head_sha:
            payload["sha"] = expected_head_sha
        result = self._request(
            "PUT",
            f"/repos/{self.repository}/pulls/{ref.pr_number}/merge",
            payload,
        )
        if not bool(result.get("merged")):
            raise GitHubWorkTrackingError(str(result.get("message") or "GitHub did not merge the pull request"))
        self._close_completed_issue(ref)
        return str(result.get("sha") or expected_head_sha or "")

    def comment(self, number: int, body: str) -> None:
        """Comment on an issue or pull request (#396: review results)."""
        self._request("POST", f"/repos/{self.repository}/issues/{number}/comments", {"body": body[:60000]})

    def _close_completed_issue(self, ref: GitHubWorkRef) -> None:
        path = f"/repos/{self.repository}/issues/{ref.issue_number}"
        try:
            self._request("PATCH", path, {"state": "closed", "state_reason": "completed"})
        except GitHubWorkTrackingError:
            # The merge's "Fixes #N" can close the issue first; GitHub then
            # answers 422 "closed by another request". Closed is closed.
            if str((self._request("GET", path) or {}).get("state")) != "closed":
                raise

    def _remote_branch_sha(self, branch_name: str) -> str:
        quoted = urllib.parse.quote(branch_name, safe="")
        result = self._request("GET", f"/repos/{self.repository}/git/ref/heads/{quoted}")
        sha = str(((result.get("object") or {}).get("sha") or "")).strip()
        if not sha:
            raise GitHubWorkTrackingError(f"could not resolve Issue branch {branch_name}")
        return sha

    @staticmethod
    def _status_paths(raw: str) -> list[str]:
        parts = raw.split("\0")
        paths: list[str] = []
        index = 0
        while index < len(parts):
            record = parts[index]
            index += 1
            if not record:
                continue
            if len(record) < 4:
                raise GitHubWorkTrackingError("invalid git status record")
            code = record[:2]
            paths.append(record[3:])
            if "R" in code or "C" in code:
                if index < len(parts) and parts[index]:
                    paths.append(parts[index])
                index += 1
        return list(dict.fromkeys(paths))

    @staticmethod
    def _publishable_path(path: str) -> bool:
        normalized = path.replace("\\", "/").lstrip("./")
        blocked = (
            "communication/",
            "state/",
            "logs/",
            ".git/",
            ".venv/",
            "__pycache__/",
        )
        name = Path(normalized).name.lower()
        secret_name = (
            name == ".env"
            or name.startswith(".env.")
            or name.endswith((".pem", ".key", ".p12", ".pfx"))
        )
        return (
            bool(normalized)
            and not normalized.startswith(blocked)
            and not normalized.endswith(".pyc")
            and not secret_name
        )

    @staticmethod
    def _git(
        args: list[str],
        *,
        cwd: str,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:2000]
            raise GitHubWorkTrackingError(f"git {' '.join(args[:2])} failed: {detail}")
        return proc

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
        try:
            self._request(
                "POST",
                f"/repos/{self.repository}/git/refs",
                {"ref": f"refs/heads/{branch_name}", "sha": sha},
            )
        except GitHubWorkTrackingError as exc:
            # The branch exists (a retry, #374, or a lookup that missed it):
            # that is the state this method ensures, not a failure.
            if "HTTP 422" not in str(exc) or "Reference already exists" not in str(exc):
                raise

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
                    pr_number, checkpoint_sha, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(experiment_id) DO UPDATE SET
                    issue_number=excluded.issue_number,
                    issue_title=excluded.issue_title,
                    branch_name=excluded.branch_name,
                    pr_number=COALESCE(excluded.pr_number, github_experiment_links.pr_number),
                    checkpoint_sha=COALESCE(excluded.checkpoint_sha, github_experiment_links.checkpoint_sha),
                    updated_at=excluded.updated_at
                """,
                (
                    ref.experiment_id,
                    ref.issue_number,
                    ref.issue_title,
                    ref.branch_name,
                    ref.pr_number,
                    ref.checkpoint_sha,
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
