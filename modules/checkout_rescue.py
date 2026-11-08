"""Self-healing preparation of a deployment checkout before a refresh.

Work (tracked/staged changes and unrecognized untracked files) is committed onto
a rescue branch ``nexus/rescue/<timestamp>-<head>``; recognizable runtime
artifacts are archived outside the repository. Anything unrecognized counts as
work, because preserving junk is cheap and deleting something that matters is not.

Safety contract: no ``git reset --hard``, no force checkout/push, no silent
delete; archiving rolls back on failure, so a failed rescue leaves the checkout
exactly as it was; repeating a rescue is a no-op (the archive is deduplicated).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable, Iterable, Sequence

from modules.redaction import redact_secrets

logger = logging.getLogger(__name__)

DEFAULT_RUNTIME_ARCHIVE_ROOT = "/var/lib/nexus/runtime-archive"
RUNTIME_ARCHIVE_ROOT_ENV = "NEXUS_RUNTIME_ARCHIVE_DIR"
EXTRA_ARTIFACT_PATTERNS_ENV = "NEXUS_RUNTIME_ARTIFACT_PATTERNS"

_ARCHIVE_INDEX_NAME = "archive-index.json"
_GIT_TIMEOUT = 60

#: Untracked paths matching one of these are Nexus runtime output, archived
#: outside the repository instead of committed. Matching is fnmatch against the
#: repository-relative POSIX path, where ``*`` also crosses separators.
RUNTIME_ARTIFACT_PATTERNS: tuple[str, ...] = (
    # Legacy in-repository mailbox drops; the mailbox lives in its own repo now.
    "communication/outbox/*",
    "communication/inbox/*",
    # Runtime state, scoped to state/ on purpose: a `.lock`/`.jsonl` elsewhere is
    # as likely to be source (lockfile, fixture) as output.
    "state/*",
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "*.sqlite",
    "*.sqlite3",
    "logs/*",
    "*.log",
    "*.tmp",
    "*.temp",
    "*.bak",
    "*.orig",
    "*.rej",
    "*~",
    "__pycache__/*",
    "*/__pycache__/*",
    "*.pyc",
    ".pytest_cache/*",
    ".mypy_cache/*",
    ".ruff_cache/*",
    "htmlcov/*",
    ".coverage",
)


class RescueError(RuntimeError):
    """Preparation failed; the checkout must be left alone and refresh aborted."""


@dataclass(frozen=True, slots=True)
class CheckoutState:
    """What the working tree looked like before anything was touched."""

    branch: str
    head: str
    staged: tuple[str, ...] = ()
    tracked: tuple[str, ...] = ()
    untracked: tuple[str, ...] = ()
    ahead: int = 0
    behind: int = 0
    detached: bool = False

    @property
    def clean(self) -> bool:
        return not (self.staged or self.tracked or self.untracked)

    def to_dict(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "head": self.head,
            "detached": self.detached,
            "clean": self.clean,
            "staged": list(self.staged),
            "tracked": list(self.tracked),
            "untracked": list(self.untracked),
            "ahead": self.ahead,
            "behind": self.behind,
        }


@dataclass(frozen=True, slots=True)
class ArchivedFile:
    """One runtime artifact moved out of the repository."""

    repo_path: str
    archived_path: str
    deduplicated: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "repo_path": self.repo_path,
            "archived_path": self.archived_path,
            "deduplicated": self.deduplicated,
        }


@dataclass(frozen=True, slots=True)
class RescueReport:
    """Exactly what was rescued and archived, for the refresh result."""

    performed: bool = False
    rescue_branch: str | None = None
    rescue_commit: str | None = None
    rescued_paths: tuple[str, ...] = ()
    archived: tuple[ArchivedFile, ...] = ()
    archive_dir: str | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "performed": self.performed,
            "rescue_branch": self.rescue_branch,
            "rescue_commit": self.rescue_commit,
            "rescued_paths": list(self.rescued_paths),
            "archived": [item.to_dict() for item in self.archived],
            "archive_dir": self.archive_dir,
            "notes": list(self.notes),
        }


def runtime_artifact_patterns(extra: Iterable[str] | None = None) -> tuple[str, ...]:
    """Built-in patterns plus deployment-specific ones from the environment."""
    patterns = list(RUNTIME_ARTIFACT_PATTERNS)
    configured = os.environ.get(EXTRA_ARTIFACT_PATTERNS_ENV, "")
    for candidate in list(configured.split(",")) + list(extra or ()):
        cleaned = candidate.strip()
        if cleaned and cleaned not in patterns:
            patterns.append(cleaned)
    return tuple(patterns)


def is_runtime_artifact(path: str, patterns: Sequence[str] | None = None) -> bool:
    """True if *path* (repository-relative) is Nexus runtime output."""
    candidate = Path(path).as_posix().lstrip("./")
    for pattern in patterns if patterns is not None else runtime_artifact_patterns():
        if fnmatch(candidate, pattern):
            return True
    return False


def classify_untracked(
    paths: Iterable[str], patterns: Sequence[str] | None = None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split untracked paths into ``(runtime_artifacts, work)``; unknown files land
    in ``work`` on purpose - preserved on the rescue branch, not archived away."""
    resolved = patterns if patterns is not None else runtime_artifact_patterns()
    artifacts = tuple(p for p in paths if is_runtime_artifact(p, resolved))
    work = tuple(p for p in paths if p not in artifacts)
    return artifacts, work


def git(
    repo_root: Path | str,
    args: Sequence[str],
    *,
    timeout: int = _GIT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Run one git command in *repo_root*; never through a shell."""
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def git_output(repo_root: Path | str, args: Sequence[str], *, what: str) -> str:
    """Run one git command and return stdout, raising :class:`RescueError`."""
    result = git(repo_root, args)
    if result.returncode != 0:
        raise RescueError(f"{what}: {redact_secrets(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout


def _parse_status(raw: str) -> tuple[list[str], list[str], list[str], str | None]:
    """Parse ``git status --porcelain=v1 -b -z -uall`` output into (staged, tracked,
    untracked, branch_header); NUL-separated so odd paths survive intact."""
    staged: list[str] = []
    tracked: list[str] = []
    untracked: list[str] = []
    branch_header: str | None = None

    tokens = [token for token in raw.split("\0") if token != ""]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token.startswith("## "):
            branch_header = token[3:]
            continue
        if len(token) < 3:
            continue
        index_status, worktree_status, path = token[0], token[1], token[3:]
        if index_status == "?" and worktree_status == "?":
            untracked.append(path)
            continue
        if index_status == "R" or index_status == "C":
            # The rename/copy source follows as its own NUL-separated token.
            if index < len(tokens):
                staged.append(tokens[index])
                index += 1
        if index_status not in (" ", "?", "!"):
            staged.append(path)
        if worktree_status not in (" ", "?", "!"):
            tracked.append(path)
    return staged, tracked, untracked, branch_header


def inspect_checkout(repo_root: Path | str, *, remote: str = "origin") -> CheckoutState:
    """Describe branch, local changes and ahead/behind state of *repo_root*."""
    raw = git_output(
        repo_root,
        ["status", "--porcelain=v1", "-b", "-z", "--untracked-files=all"],
        what="could not read the checkout status",
    )
    staged, tracked, untracked, header = _parse_status(raw)

    head = git_output(repo_root, ["rev-parse", "HEAD"], what="could not read HEAD").strip()
    branch_result = git(repo_root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    detached = branch_result.returncode != 0
    if detached:
        branch = (header or "HEAD (no branch)").split(" ")[0]
    else:
        branch = branch_result.stdout.strip()

    ahead = behind = 0
    if not detached:
        counts = git(
            repo_root,
            ["rev-list", "--left-right", "--count", f"{branch}...{remote}/{branch}"],
        )
        if counts.returncode == 0:
            parts = counts.stdout.split()
            if len(parts) == 2 and all(part.isdigit() for part in parts):
                ahead, behind = int(parts[0]), int(parts[1])

    return CheckoutState(
        branch=branch,
        head=head,
        staged=tuple(dict.fromkeys(staged)),
        tracked=tuple(dict.fromkeys(tracked)),
        untracked=tuple(untracked),
        ahead=ahead,
        behind=behind,
        detached=detached,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ArchiveIndex:
    """Content index keyed by ``<relative path>:<sha256>``, so a retry reuses an
    already-archived copy instead of filling the archive with duplicates."""

    def __init__(self, root: Path) -> None:
        self._path = root / _ARCHIVE_INDEX_NAME
        self._entries: dict[str, str] = {}
        if self._path.is_file():
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                entries = loaded.get("entries", {})
                if isinstance(entries, dict):
                    self._entries = {str(k): str(v) for k, v in entries.items()}
            except (OSError, ValueError, AttributeError):
                logger.warning("runtime archive index unreadable; starting a new one")
                self._entries = {}

    @staticmethod
    def key(repo_relative: str, digest: str) -> str:
        return f"{repo_relative}:{digest}"

    def lookup(self, repo_relative: str, digest: str) -> Path | None:
        recorded = self._entries.get(self.key(repo_relative, digest))
        if not recorded:
            return None
        existing = Path(recorded)
        if not existing.is_file():
            return None
        try:
            if _sha256(existing) != digest:
                return None
        except OSError:
            return None
        return existing

    def record(self, repo_relative: str, digest: str, archived: Path) -> None:
        self._entries[self.key(repo_relative, digest)] = str(archived)

    def save(self) -> None:
        payload = json.dumps({"entries": self._entries}, indent=2, sort_keys=True)
        temp = self._path.with_suffix(".tmp")
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, self._path)


class _ArchiveTransaction:
    """Archive moves that stay provisional until the whole rescue succeeds: until
    :meth:`commit`, the index is unsaved and parked duplicates are only held,
    never deleted - a failed rescue puts every artifact back, leaving the
    checkout exactly as it was."""

    def __init__(self, index: ArchiveIndex | None = None, archive_dir: Path | None = None) -> None:
        self._index = index
        self.archive_dir = archive_dir
        self.archived: list[ArchivedFile] = []
        self._moved: list[tuple[Path, Path]] = []
        self._parked: list[tuple[Path, Path]] = []

    def record_move(self, source: Path, destination: Path, relative: str, digest: str) -> None:
        self._moved.append((source, destination))
        if self._index is not None:
            self._index.record(relative, digest, destination)
        self.archived.append(ArchivedFile(repo_path=relative, archived_path=str(destination)))

    def record_duplicate(self, source: Path, parked: Path, relative: str, existing: Path) -> None:
        self._parked.append((source, parked))
        self.archived.append(
            ArchivedFile(repo_path=relative, archived_path=str(existing), deduplicated=True)
        )

    def rollback(self) -> None:
        """Put every moved file back where it came from."""
        for source, destination in reversed(self._moved):
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(destination), str(source))
            except OSError:  # pragma: no cover - best effort rollback
                logger.error("could not restore %s after a failed rescue", source)
        for source, parked in reversed(self._parked):
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(parked), str(source))
            except OSError:  # pragma: no cover - best effort rollback
                logger.error("could not restore duplicate artifact %s", source)
        self._moved.clear()
        self._parked.clear()

    def commit(self) -> None:
        """Persist the index and drop the parked duplicate originals."""
        if self._index is not None and self._moved:
            self._index.save()
        holding_dirs: set[Path] = set()
        for _source, parked in self._parked:
            holding_dirs.add(parked.parent)
            try:
                parked.unlink()
            except OSError:  # pragma: no cover - the copy is still recoverable
                logger.warning("duplicate artifact copy left behind at %s", parked)
        for holding in holding_dirs:
            try:
                holding.rmdir()
            except OSError:  # pragma: no cover - not empty, so something is left to keep
                pass
        self._moved.clear()
        self._parked.clear()


class CheckoutRescue:
    """Bring a dirty deployment checkout back to a clean, deployable state."""

    def __init__(
        self,
        repo_root: Path | str,
        *,
        target_branch: str = "main",
        archive_root: Path | str | None = None,
        extra_artifact_patterns: Iterable[str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._repo_root = Path(repo_root).resolve()
        self._target_branch = target_branch
        self._archive_root = Path(
            archive_root
            or os.environ.get(RUNTIME_ARCHIVE_ROOT_ENV)
            or DEFAULT_RUNTIME_ARCHIVE_ROOT
        )
        self._patterns = runtime_artifact_patterns(extra_artifact_patterns)
        self._clock = clock or time.time

    @property
    def archive_root(self) -> Path:
        return self._archive_root

    # Public API

    def prepare(self, state: CheckoutState | None = None) -> RescueReport:
        """Preserve local changes, then leave the worktree clean; raises
        :class:`RescueError` without changing anything the caller cannot see
        when preservation is not possible."""
        current = state or inspect_checkout(self._repo_root)
        if current.clean:
            return RescueReport(notes=("checkout was clean; nothing to rescue",))

        timestamp = time.strftime("%Y%m%d%H%M%S", time.gmtime(self._clock()))
        artifacts, work = classify_untracked(current.untracked, self._patterns)
        # A symlink is never archived, whatever it is named: moving it could follow
        # it out of the repository or leave a dangling link, and skipping it would
        # leave the worktree dirty. Git stores the link itself, so the rescue
        # branch is the honest place for it.
        symlinks = tuple(path for path in artifacts if (self._repo_root / path).is_symlink())
        if symlinks:
            artifacts = tuple(path for path in artifacts if path not in symlinks)
            work = (*work, *symlinks)
        work_paths = tuple(dict.fromkeys((*current.staged, *current.tracked, *work)))

        # Archive first, before any git mutation, so a broken archive aborts with
        # the checkout still exactly as it was; moves stay provisional and later
        # failures put the artifacts back.
        transaction = self._archive_runtime_artifacts(artifacts, timestamp)

        notes: list[str] = []
        if symlinks:
            notes.append(f"preserved {len(symlinks)} symlink(s) on the rescue branch")
        rescue_branch: str | None = None
        rescue_commit: str | None = None
        try:
            if work_paths:
                rescue_branch, rescue_commit, note = self._commit_work(
                    work_paths, current, timestamp
                )
                if note:
                    notes.append(note)
            else:
                notes.append("no source or work changes; nothing was committed")

            remaining = inspect_checkout(self._repo_root)
            if not remaining.clean:
                raise RescueError(
                    "checkout is still dirty after rescue: "
                    + ", ".join((*remaining.staged, *remaining.tracked, *remaining.untracked))[:300]
                )
        except BaseException:
            # An existing rescue commit is left alone - deleting it would destroy
            # the work it preserves; the archive is what gets undone.
            transaction.rollback()
            raise

        transaction.commit()
        return RescueReport(
            performed=True,
            rescue_branch=rescue_branch,
            rescue_commit=rescue_commit,
            # Only claim paths that a commit actually preserved.
            rescued_paths=work_paths if rescue_commit else (),
            archived=tuple(transaction.archived),
            archive_dir=str(transaction.archive_dir) if transaction.archive_dir else None,
            notes=tuple(notes),
        )

    def preserve_commits(self, head: str, *, reason: str = "diverged deployment branch") -> str:
        """Point a rescue branch at *head* so local commits that block a
        fast-forward stay reachable - naming them is what makes moving the
        branch reference non-destructive."""
        timestamp = time.strftime("%Y%m%d%H%M%S", time.gmtime(self._clock()))
        branch = self._rescue_branch_name(head, timestamp)
        result = git(self._repo_root, ["branch", branch, head])
        if result.returncode != 0:
            raise RescueError(
                f"could not preserve commits of {reason}: "
                f"{redact_secrets(result.stderr).strip()[:300]}"
            )
        return branch

    # Archiving

    def _archive_runtime_artifacts(
        self, artifacts: Sequence[str], timestamp: str
    ) -> _ArchiveTransaction:
        """Move runtime artifacts aside, undoably. Nothing is persisted yet."""
        if not artifacts:
            return _ArchiveTransaction()

        try:
            self._archive_root.mkdir(parents=True, exist_ok=True)
            archive_dir = self._archive_root / timestamp
            archive_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RescueError(f"runtime archive directory is unusable: {exc}") from exc

        try:
            index = ArchiveIndex(self._archive_root)
        except OSError as exc:  # pragma: no cover - defensive
            raise RescueError(f"runtime archive index is unusable: {exc}") from exc

        transaction = _ArchiveTransaction(index, archive_dir)
        try:
            for relative in artifacts:
                source = self._repo_root / relative
                if not source.is_file() or source.is_symlink():
                    # Nothing to move (already gone, or a link we will not follow).
                    continue
                digest = _sha256(source)
                existing = index.lookup(relative, digest)
                if existing is not None:
                    # Identical content already archived; a second copy would
                    # accumulate on retries.
                    parked = self._duplicate_backup(source, timestamp)
                    shutil.move(str(source), str(parked))
                    transaction.record_duplicate(source, parked, relative, existing)
                    continue
                destination = self._unique_destination(archive_dir / relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                transaction.record_move(source, destination, relative, digest)
        except (OSError, RescueError) as exc:
            transaction.rollback()
            raise RescueError(f"archiving runtime artifacts failed: {exc}") from exc
        return transaction

    def _duplicate_backup(self, source: Path, timestamp: str) -> Path:
        """Staging path for an artifact whose content is already archived; moved
        aside, not deleted, so a later failure in the same run can put it back."""
        holding = self._archive_root / f".duplicates-{timestamp}"
        holding.mkdir(parents=True, exist_ok=True)
        return self._unique_destination(holding / source.name)

    @staticmethod
    def _unique_destination(candidate: Path) -> Path:
        if not candidate.exists():
            return candidate
        stem, suffix = candidate.stem, candidate.suffix
        for counter in range(1, 1000):
            alternative = candidate.with_name(f"{stem}-{counter}{suffix}")
            if not alternative.exists():
                return alternative
        raise RescueError(f"could not find a free archive name for {candidate.name}")

    # Rescue branch

    def _commit_work(
        self, work_paths: Sequence[str], state: CheckoutState, timestamp: str
    ) -> tuple[str | None, str | None, str | None]:
        """Commit *work_paths* onto a rescue branch; return branch, commit, note."""
        branch = self._rescue_branch_name(state.head, timestamp)
        result = git(self._repo_root, ["switch", "--create", branch])
        if result.returncode != 0:
            raise RescueError(
                f"could not create rescue branch: {redact_secrets(result.stderr).strip()[:300]}"
            )

        try:
            add = git(self._repo_root, ["add", "--all", "--", *work_paths])
            if add.returncode != 0:
                raise RescueError(
                    f"could not stage rescued work: {redact_secrets(add.stderr).strip()[:300]}"
                )
            message = (
                f"nexus: rescue local changes from {state.branch} before refresh\n\n"
                f"Preserved {len(work_paths)} path(s) found in the live checkout at "
                f"{state.head[:12]}.\n"
                "Committed by the Nexus refresh self-heal so the deployment branch "
                "could be fast-forwarded without discarding local work."
            )
            commit = git(
                self._repo_root,
                [*self._identity_args(), "commit", "--no-verify", "-m", message],
            )
            if commit.returncode != 0:
                if self._worktree_is_clean():
                    # Stat-only changes with identical content: nothing to commit;
                    # the clean worktree is all the refresh needs - an empty rescue
                    # branch would only be noise.
                    self._discard_empty_branch(branch)
                    return None, None, (
                        "local changes were stat-only; no rescue commit was needed"
                    )
                raise RescueError(
                    f"could not commit rescued work: "
                    f"{redact_secrets(commit.stderr or commit.stdout).strip()[:300]}"
                )
            commit_sha = git_output(
                self._repo_root, ["rev-parse", "HEAD"], what="could not read the rescue commit"
            ).strip()
        except RescueError:
            # Put the checkout back on the branch it came from; working-tree
            # changes travel with it, so nothing is lost.
            git(self._repo_root, ["switch", state.branch])
            git(self._repo_root, ["branch", "--delete", "--force", branch])
            raise

        switch_back = git(self._repo_root, ["switch", self._target_branch])
        if switch_back.returncode != 0:
            raise RescueError(
                f"rescued work is on {branch}, but switching back to "
                f"{self._target_branch} failed: "
                f"{redact_secrets(switch_back.stderr).strip()[:300]}"
            )
        return branch, commit_sha, None

    def _worktree_is_clean(self) -> bool:
        result = git(self._repo_root, ["status", "--porcelain", "--untracked-files=all"])
        return result.returncode == 0 and not result.stdout.strip()

    def _discard_empty_branch(self, branch: str) -> None:
        """Drop a rescue branch that never received a commit; it points at the
        same commit as the deployment branch, so the delete loses nothing."""
        switch_back = git(self._repo_root, ["switch", self._target_branch])
        if switch_back.returncode != 0:
            raise RescueError(
                f"could not switch back to {self._target_branch}: "
                f"{redact_secrets(switch_back.stderr).strip()[:300]}"
            )
        git(self._repo_root, ["branch", "--delete", branch])

    def _rescue_branch_name(self, head: str, timestamp: str) -> str:
        base = f"nexus/rescue/{timestamp}-{head[:8] or 'unknown'}"
        candidate = base
        for counter in range(1, 1000):
            exists = git(self._repo_root, ["rev-parse", "--verify", "--quiet", candidate])
            if exists.returncode != 0:
                return candidate
            candidate = f"{base}-{counter}"
        raise RescueError("could not find a free rescue branch name")

    def _identity_args(self) -> list[str]:
        """Fall back to a Nexus identity only when git has none configured."""
        configured = git(self._repo_root, ["config", "--get", "user.email"])
        if configured.returncode == 0 and configured.stdout.strip():
            return []
        return [
            "-c",
            "user.name=Nexus Refresh",
            "-c",
            "user.email=nexus-refresh@localhost",
        ]
