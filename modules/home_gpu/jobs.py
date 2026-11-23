"""Submit batch jobs for the home GPU through the shared mailbox (#759).

``submit_job`` writes ``inbox/<job_id>.md`` into the existing mailbox working
copy::

    ---
    type: batch_job
    to: home-gpu
    created_by: nexus
    job_id: <job_id>
    kind: <kind>
    deadline: <ISO UTC>
    input_sha256: <sha256 of the body>
    ---
    <payload as deterministic JSON>

Because of ``to: home-gpu`` Nexus' own mailbox processor leaves the file alone
(``modules.mailbox.routing``). The payload leaves the VM in a git repository,
so ``check_payload`` only lets public, repo-relative data through: at most
64 KB, no absolute path, no ``..`` path segment, no URL except this repo on
GitHub and nothing the mailbox secret scanner flags.

Commit and push reuse the mailbox git helpers (``git_add_and_commit_paths``
and ``git_push`` from ``modules.mailbox.core``); this module has no git code
except the rollback reset and the undo of an unpublished commit. The whole
feature is off unless ``NEXUS_HOME_GPU=1``.

Submission is atomic (#812, review on #774): inside one try block the inbox
file is written, staged and committed - the commit scoped to the job path
only, so unrelated staged entries and a concurrent submission's staged job
are not published - and only then is the ``queued`` row inserted into
``home_gpu_jobs`` in the run store. If staging or the commit fails, the inbox
file (and any ``.tmp-*`` leftover of the atomic write) is removed, ``git
reset`` unstages the path, no row is written and a ``HomeGpuSubmitError``
reports any rollback leftover, so the mailbox poll sees no change and a
retry gets a fresh job ID without a stray file or orphan row. With
``commit=False`` the submit stays file-only: no commit, no push and no row;
the row is written by :func:`recover_unpublished_jobs` once the caller's own
commit is in git. If the row insert fails after a confirmed commit, the
still-local commit is undone (``git reset --soft HEAD~1`` + path unstage +
file removal) whenever HEAD is exactly the job commit; otherwise the job is
not pushed and the error names the job ID so bookkeeping can resume under
that same ID before the poll's sync could publish it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import modules.run_store as run_store
from modules.mailbox import core as mailbox_core

logger = logging.getLogger(__name__)

ENV_FLAG = "NEXUS_HOME_GPU"
RECIPIENT = "home-gpu"
JOB_TYPE = "batch_job"
CREATED_BY = "nexus"
ALLOWED_KINDS = frozenset(
    {"chat_completion", "hunk_replay", "bakeoff_prompt", "review_draft", "code_draft"}
)
MAX_PAYLOAD_BYTES = 64 * 1024
DEFAULT_DEADLINE_S = 6 * 3600
ALLOWED_URL_PREFIX = "https://github.com/tire0011/agent-lab"

# Anything with a scheme (``https://``, ``ssh://``, ``file://`` ...) or a bare
# ``www.`` host counts as a URL.
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>()\[\]{}]*|\bwww\.[^\s\"'<>()\[\]{}]+")
# This repo only - ``agent-lab-other`` or ``agent-lab.evil`` do not pass.
_ALLOWED_URL = re.compile(
    re.escape(ALLOWED_URL_PREFIX) + r"(?:\.git)?(?:[/?#][^\s]*)?$"
)
# POSIX absolute (``/etc``), home (``~/x``), Windows drive (``C:\``, ``C:/``) or
# UNC (``\\host``) path at the start of a token. ``a/b``, ``x / y`` and ``//``
# comments do not match.
_ABS_PATH = re.compile(r"(?<![\w.~/\\-])(?:~?/|[A-Za-z]:[\\/]|\\\\)(?=[\w.~$-])")
# ``..`` as a path segment or token (``../x``, ``a/../b``, ``..``), not the
# dots of an ellipsis or of ``a..b``.
_DOT_DOT = re.compile(r"(?<![^\s/\\\"'=:(,])\.\.(?![^\s/\\\"'),])")


class HomeGpuJobError(Exception):
    """A home-GPU job could not be submitted."""


class HomeGpuDisabled(HomeGpuJobError):
    """``NEXUS_HOME_GPU`` is not ``1``; nothing was written."""


class PayloadRejected(HomeGpuJobError, ValueError):
    """The payload is not public, repo-relative data; nothing was written."""


class HomeGpuSubmitError(HomeGpuJobError):
    """Publishing the job to the mailbox failed (#812, review on #774).

    When staging or the git commit fails, the inbox file and any ``.tmp-*``
    leftover are removed, the path is unstaged with ``git reset`` and no
    ``queued`` row is written; any cleanup that could not be completed is
    named in the message instead of being silently dropped. A job whose
    commit succeeded but whose row insert failed is reported here too: the
    unpublished commit is undone when HEAD is exactly the job commit, and
    otherwise the message names the job ID whose bookkeeping must resume
    before the job is pushed.
    """


class _PublishFailed(Exception):
    """Internal marker: staging or the path-scoped commit failed.

    The outer handler of ``submit_job`` turns this into a rollback plus a
    ``HomeGpuSubmitError``; it never escapes the module.
    """


@dataclass(frozen=True)
class SubmittedJob:
    job_id: str
    kind: str
    path: Path
    deadline: str
    input_sha256: str
    committed: bool
    pushed: bool


def home_gpu_enabled() -> bool:
    """True only for ``NEXUS_HOME_GPU=1``; default off."""
    return os.environ.get(ENV_FLAG, "").strip() == "1"


def _serialize(payload: Any) -> str:
    try:
        return json.dumps(
            payload, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2
        )
    except (TypeError, ValueError) as exc:
        raise PayloadRejected(f"payload is not plain JSON: {exc}") from None


def _strings(value: Any) -> Iterator[str]:
    """Every string in *value*, dict keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            if isinstance(item, (str, int, float)) and not isinstance(item, bool):
                # ``{"API_KEY": "x"}`` reads as ``API_KEY: x`` for the scanner.
                yield f"{key}: {item}"
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _check_text(text: str) -> None:
    for match in _URL.finditer(text):
        if not _ALLOWED_URL.match(match.group(0)):
            raise PayloadRejected(
                f"payload contains a URL outside {ALLOWED_URL_PREFIX}"
            )
    if _DOT_DOT.search(text):
        raise PayloadRejected("payload contains a '..' path segment")
    if _ABS_PATH.search(_URL.sub(" ", text)):
        raise PayloadRejected("payload contains an absolute path")
    if mailbox_core._has_secret(text):
        raise PayloadRejected("payload matches a secret pattern")


def _check(payload: Any) -> str:
    body = _serialize(payload)
    size = len(body.encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        raise PayloadRejected(
            f"payload is {size} bytes as JSON, limit {MAX_PAYLOAD_BYTES}"
        )
    for text in _strings(payload):
        _check_text(text)
    # And the text that actually leaves the VM.
    if mailbox_core._has_secret(body):
        raise PayloadRejected("payload matches a secret pattern")
    return body


def check_payload(payload: Any) -> None:
    """Raise ``PayloadRejected`` unless *payload* is safe to publish."""
    _check(payload)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_job_id(now: datetime) -> str:
    return f"hg-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"


def _rollback_publication(
    repo: Path, path: Path, *, remove_inbox: bool, attempts: int = 2
) -> list[str]:
    """Undo a job file that could not be published (#812, review on #774).

    Removes the inbox file and any stale ``<job>.md.tmp-*`` leftover of
    ``_atomic_write``, then unstages exactly the job path with
    ``git reset -- <path>`` - other staged entries are preserved. Transient
    failures are retried; whatever still remains (file present, reset
    failed, path still staged) is returned so the caller reports it in
    ``HomeGpuSubmitError`` instead of claiming a clean rollback. Never
    raises: an empty list means the rollback fully succeeded.
    """
    leftovers: list[str] = []
    try:
        rel: str | None = str(path.relative_to(repo))
    except ValueError:
        rel = None
        leftovers.append(f"cannot relativize {path} against {repo}")

    targets = [path, *sorted(path.parent.glob(f"{path.name}.tmp-*"))]
    for target in targets:
        gone = False
        for _ in range(attempts):
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("home-gpu rollback: could not remove %s: %s", target, exc)
                continue
            if not target.exists():
                gone = True
                break
        if not gone:
            leftovers.append(f"file still present: {target.name}")

    if rel is not None:
        reset_ok = False
        for _ in range(attempts):
            try:
                result = mailbox_core._git(["reset", "--", str(rel)], repo, check=False)
            except Exception as exc:  # rollback must never raise
                logger.warning("home-gpu rollback: git reset of %s raised: %s", rel, exc)
                continue
            if result.returncode == 0:
                reset_ok = True
                break
            logger.warning(
                "home-gpu rollback: git reset of %s failed: %s",
                rel, mailbox_core.redact(result.stderr.strip())[:200],
            )
        if not reset_ok:
            leftovers.append(f"git reset of {rel} failed")
        try:
            staged = mailbox_core._git(
                ["diff", "--cached", "--name-only", "--", str(rel)], repo, check=False
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("home-gpu rollback: staged check raised: %s", exc)
            staged = None
        if staged is not None and (staged.returncode != 0 or staged.stdout.strip()):
            leftovers.append(f"path still staged: {rel}")

    if remove_inbox:
        try:
            (repo / "inbox").rmdir()
        except OSError:
            pass  # not empty or already gone; an empty inbox dir is harmless
    return leftovers


def _head_commit(repo: Path) -> tuple[str, list[str]] | None:
    """(subject, changed files) of HEAD, or None when git cannot answer."""
    subject = mailbox_core._git(["log", "-1", "--format=%s"], repo, check=False)
    files = mailbox_core._git(
        ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"], repo, check=False
    )
    if subject.returncode != 0 or files.returncode != 0:
        return None
    return subject.stdout.strip(), files.stdout.split()


def _undo_unpublished_commit(
    repo: Path, path: Path, message: str, *, remove_inbox: bool, attempts: int = 2
) -> list[str]:
    """Undo a local-only commit of exactly the job path (#812, review on #774).

    Safe only while HEAD is precisely our path-scoped commit (same subject and
    only the job file changed) and a parent commit exists: ``git reset
    --soft HEAD~1`` moves HEAD back while keeping the index, then the path is
    unstaged and the file removed exactly like a publication rollback, so
    unrelated staged entries survive. The commit is local-only here - the
    push happens only after the row insert - so the mailbox poll cannot have
    published it. Returns leftover problems; empty means fully undone.
    """
    try:
        rel = str(path.relative_to(repo))
    except ValueError as exc:
        return [f"cannot relativize {path} against {repo}: {exc}"]
    try:
        head = _head_commit(repo)
        parent = mailbox_core._git(["rev-parse", "--verify", "HEAD~1"], repo, check=False)
    except Exception as exc:  # undo must never raise
        return [f"git inspection failed: {mailbox_core.redact(str(exc))[:200]}"]
    if head is None or head[0] != message or head[1] != [rel]:
        return ["HEAD is not the job's own commit; the commit was left in place"]
    if parent.returncode != 0:
        return ["the job commit has no parent; the commit was left in place"]
    for _ in range(attempts):
        try:
            soft = mailbox_core._git(["reset", "--soft", "HEAD~1"], repo, check=False)
        except Exception as exc:  # undo must never raise
            logger.warning("home-gpu undo: git reset --soft raised: %s", exc)
            continue
        if soft.returncode == 0:
            break
        logger.warning(
            "home-gpu undo: git reset --soft failed: %s",
            mailbox_core.redact(soft.stderr.strip())[:200],
        )
    else:
        return ["git reset --soft HEAD~1 failed; the commit was left in place"]
    leftovers = _rollback_publication(repo, path, remove_inbox=remove_inbox, attempts=attempts)
    try:
        now = mailbox_core._git(["rev-parse", "HEAD"], repo, check=False)
    except Exception as exc:  # pragma: no cover - defensive
        now = None
        leftovers.append(f"HEAD re-check raised: {mailbox_core.redact(str(exc))[:200]}")
    if now is not None and (now.returncode != 0 or now.stdout.strip() != parent.stdout.strip()):
        leftovers.append("HEAD did not move back after git reset --soft")
    return leftovers


def _write_queued_row(
    db_path: str | Path | None,
    *,
    job_id: str,
    kind: str,
    submitted_at: str,
    deadline: str,
    input_sha256: str,
) -> None:
    """Insert the ``queued`` row; called only after the commit is confirmed."""
    conn = run_store.init_db(
        str(db_path) if db_path is not None else run_store.DEFAULT_DB_PATH
    )
    try:
        run_store.insert_home_gpu_job(
            conn,
            job_id=job_id,
            kind=kind,
            submitted_at=submitted_at,
            deadline=deadline,
            input_sha256=input_sha256,
        )
    finally:
        conn.close()


def _commit_time_iso(repo: Path, rel: str) -> str:
    """ISO time of the last commit that touched *rel* (the submit commit)."""
    result = mailbox_core._git(["log", "-1", "--format=%cI", "--", rel], repo, check=False)
    if result.returncode == 0 and result.stdout.strip():
        try:
            return _iso(datetime.fromisoformat(result.stdout.strip()))
        except ValueError:
            pass
    return _iso(datetime.now(timezone.utc))


def recover_unpublished_jobs(
    mailbox_dir: Path | str, db_path: str | Path | None = None
) -> list[str]:
    """Insert ``queued`` rows for committed job files that have none (#812).

    A submit interrupted between the git commit and the row insert leaves a
    committed inbox file without a ``home_gpu_jobs`` row - and the mailbox
    poll's own sync pushes every local commit, which would publish the job
    untracked. This scan gives each such file its row under the *same* job id
    found in its front matter, so a retry never dispatches the same work
    twice under two ids. Only files already tracked by git are considered; an
    uncommitted file belongs to a submit still in progress or to a
    ``commit=False`` caller. Returns the job ids that received a row.
    """
    repo = Path(mailbox_dir)
    inbox = repo / "inbox"
    if not inbox.is_dir():
        return []
    candidates: list[tuple[str, dict[str, str]]] = []
    for path in sorted(inbox.glob("*.md")):
        try:
            rel = str(path.relative_to(repo))
        except ValueError:
            continue
        try:
            tracked = mailbox_core._git(["ls-files", "--error-unmatch", "--", rel], repo, check=False)
        except Exception:
            continue
        if tracked.returncode != 0:
            continue  # uncommitted: not published, not ours to bookkeep yet
        try:
            meta, _body = mailbox_core.parse_front_matter(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if meta.get("type") != JOB_TYPE or meta.get("to", "").strip().lower() != RECIPIENT:
            continue
        if not all(field in meta for field in ("job_id", "kind", "deadline", "input_sha256")):
            continue
        candidates.append((rel, meta))

    recovered: list[str] = []
    if not candidates:
        return recovered
    conn = run_store.init_db(
        str(db_path) if db_path is not None else run_store.DEFAULT_DB_PATH
    )
    try:
        for rel, meta in candidates:
            job_id = meta["job_id"]
            if run_store.get_home_gpu_job(conn, job_id) is not None:
                continue
            try:
                run_store.insert_home_gpu_job(
                    conn,
                    job_id=job_id,
                    kind=meta["kind"],
                    submitted_at=_commit_time_iso(repo, rel),
                    deadline=meta["deadline"],
                    input_sha256=meta["input_sha256"],
                )
            except sqlite3.IntegrityError:
                continue  # a concurrent recovery inserted the same job id first
            recovered.append(job_id)
    finally:
        conn.close()
    if recovered:
        logger.info(
            "home-gpu: recovered queued rows for committed jobs: %s", ", ".join(recovered)
        )
    return recovered


def submit_job(
    kind: str,
    payload: Any,
    deadline_s: int = DEFAULT_DEADLINE_S,
    *,
    mailbox_dir: Path | str,
    commit: bool,
    db_path: str | Path | None = None,
) -> SubmittedJob:
    """Write a ``batch_job`` for the home GPU into ``<mailbox_dir>/inbox``.

    With *commit* the file is staged, committed (scoped to the job path only)
    and pushed through the mailbox git helpers; without it the caller commits
    and the submit stays file-only: no git call, no push and no ``queued``
    row. Every check runs before anything is written: disabled flag, unknown
    kind, bad deadline and rejected payload all raise and leave the mailbox
    untouched.

    The submission itself is atomic (#812, review on #774), inside one try
    block: the inbox file is written, staged and committed, and only then is
    the ``queued`` row inserted into ``home_gpu_jobs`` in the run store
    (*db_path*, default ``AGENT_DB_PATH``). If staging or the commit raises
    or fails, the inbox file (and any ``.tmp-*`` leftover) is removed,
    ``git reset`` unstages the path, no row is written and
    ``HomeGpuSubmitError`` is raised, naming any cleanup that could not be
    completed - the mailbox poll then sees no change. If the row insert fails
    after a confirmed commit, the still-local commit is undone when HEAD is
    exactly the job commit; otherwise the job is not pushed and the error
    names the job ID so bookkeeping resumes under it before the poll's sync
    could publish it. A failed push is still only reported via
    ``pushed=False`` (the mailbox poll retries unpushed commits); a committed
    job keeps its row.
    """
    if not home_gpu_enabled():
        raise HomeGpuDisabled(f"{ENV_FLAG}=1 is not set; home-GPU jobs are off")
    if kind not in ALLOWED_KINDS:
        raise HomeGpuJobError(
            f"unknown job kind {kind!r}; allowed: {', '.join(sorted(ALLOWED_KINDS))}"
        )
    if isinstance(deadline_s, bool) or not isinstance(deadline_s, int) or deadline_s <= 0:
        raise HomeGpuJobError(f"deadline_s must be a positive int, got {deadline_s!r}")

    body = _check(payload)

    repo = Path(mailbox_dir)
    if not repo.is_dir():
        raise HomeGpuJobError(f"mailbox working copy not found: {repo}")

    now = datetime.now(timezone.utc)
    job_id = _new_job_id(now)
    submitted_at = _iso(now)
    deadline = _iso(now + timedelta(seconds=deadline_s))
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    content = (
        "---\n"
        f"type: {JOB_TYPE}\n"
        f"to: {RECIPIENT}\n"
        f"created_by: {CREATED_BY}\n"
        f"job_id: {job_id}\n"
        f"kind: {kind}\n"
        f"deadline: {deadline}\n"
        f"input_sha256: {digest}\n"
        "---\n"
        f"{body}\n"
    )

    if commit:
        # A previous submit interrupted between its commit and its row insert
        # left a committed file without a row; heal those under their original
        # job id first, so a retry never dispatches the same work twice (#812).
        try:
            recover_unpublished_jobs(repo, db_path)
        except Exception as exc:
            logger.warning(
                "home-gpu: recovery of unpublished jobs failed: %s",
                mailbox_core.redact(str(exc))[:200],
            )

    inbox = repo / "inbox"
    created_inbox = not inbox.exists()
    inbox.mkdir(exist_ok=True)
    path = inbox / f"{job_id}.md"
    message = f"home-gpu: submit {kind} job [{job_id}]"

    committed = pushed = False
    try:
        mailbox_core._atomic_write(path, content)
        if commit:
            # Stage exactly the job path (check=True: a staging failure
            # raises) and commit only that path, so unrelated staged entries
            # and a concurrent submission's staged job stay staged and are
            # not published by this commit. A commit failure returns False;
            # both cases leave the file untracked or staged, so both are
            # publication failures that must roll the file back (#812).
            if not mailbox_core.git_add_and_commit_paths(repo, [path], message):
                raise _PublishFailed(f"git commit failed for job file {path.name}")
            committed = True
            # The queued row is written only after the commit is confirmed;
            # commit=False stays file-only and writes no row at all.
            try:
                _write_queued_row(
                    db_path,
                    job_id=job_id,
                    kind=kind,
                    submitted_at=submitted_at,
                    deadline=deadline,
                    input_sha256=digest,
                )
            except Exception as db_exc:
                detail = mailbox_core.redact(str(db_exc))[:300]
                # Recoverable partial submission: the commit is local-only
                # (the push below never ran), so undo it when HEAD is exactly
                # our job commit; otherwise leave it and say which job id the
                # bookkeeping must resume under before any push (#812).
                leftovers = _undo_unpublished_commit(
                    repo, path, message, remove_inbox=created_inbox
                )
                if leftovers:
                    raise HomeGpuSubmitError(
                        f"job {job_id} is committed in the mailbox but its queued row "
                        f"could not be written ({detail}) and the unpublished commit "
                        f"could not be undone; the job was NOT pushed - resume "
                        f"bookkeeping under job_id {job_id} (recover_unpublished_jobs) "
                        f"before any push; leftovers: {', '.join(leftovers)}"
                    ) from db_exc
                raise HomeGpuSubmitError(
                    f"the queued row for job {job_id} could not be written ({detail}); "
                    "the unpublished commit, the inbox file and any staging were "
                    "rolled back and no row exists"
                ) from db_exc
            pushed = mailbox_core.git_push(repo)
    except HomeGpuSubmitError:
        raise
    except Exception as exc:
        detail = mailbox_core.redact(str(exc))[:300]
        if committed:
            # The commit already succeeded; the file and the row stay for the
            # mailbox poll.
            raise HomeGpuSubmitError(
                f"job {job_id} is committed in the mailbox but the submit "
                f"failed afterwards: {detail}"
            ) from exc
        leftovers = _rollback_publication(repo, path, remove_inbox=created_inbox)
        note = f"; rollback leftovers: {', '.join(leftovers)}" if leftovers else ""
        raise HomeGpuSubmitError(
            f"publishing job file {path.name} failed: {detail}; "
            f"the inbox file, any staging and any queued row were rolled back{note}"
        ) from exc

    return SubmittedJob(
        job_id=job_id,
        kind=kind,
        path=path,
        deadline=deadline,
        input_sha256=digest,
        committed=committed,
        pushed=pushed,
    )
