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

Commit and push reuse the mailbox git helpers (``git_add_and_commit`` and
``git_push`` from ``modules.mailbox.core``); this module has no git code
except the rollback reset. The whole feature is off unless
``NEXUS_HOME_GPU=1``.

Submission is atomic (#812): the inbox file is written, staged and committed,
and only then is the ``queued`` row inserted into ``home_gpu_jobs`` in the run
store - all inside one try block. If staging or the commit fails, the inbox
file is removed, ``git reset`` unstages the path, no row is written and a
``HomeGpuSubmitError`` is raised, so the mailbox poll sees no change and a
retry can reuse a fresh job ID without a stray file or orphan row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
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
    """Publishing the job to the mailbox failed (#812).

    When staging or the git commit fails, the inbox file is removed, the path
    is unstaged with ``git reset`` and no ``queued`` row is written, so this
    error never leaves a stray file, staging or row behind. A job whose commit
    succeeded but whose bookkeeping failed is reported here too; its file
    stays committed for the mailbox poll to push.
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


def _rollback_publication(repo: Path, path: Path, *, remove_inbox: bool) -> None:
    """Best-effort undo of a job file that could not be published (#812).

    Removes the inbox file, then runs ``git reset`` on that path so a staged
    entry is dropped too. Never raises: a failed rollback is logged, the
    original ``HomeGpuSubmitError`` is what the caller sees.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("home-gpu rollback: could not remove %s: %s", path, exc)
    try:
        rel = path.relative_to(repo)
        mailbox_core._git(["reset", "--", str(rel)], repo, check=False)
    except (OSError, ValueError) as exc:
        logger.warning("home-gpu rollback: git reset of %s failed: %s", path, exc)
    if remove_inbox:
        try:
            (repo / "inbox").rmdir()
        except OSError:
            pass  # not empty or already gone; an empty inbox dir is harmless


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

    With *commit* the file is staged, committed and pushed through the mailbox
    git helpers; without it the caller commits. Every check runs before
    anything is written: disabled flag, unknown kind, bad deadline and
    rejected payload all raise and leave the mailbox untouched.

    The submission itself is atomic (#812), inside one try block: the inbox
    file is written, staged and committed, and only then is the ``queued`` row
    inserted into ``home_gpu_jobs`` in the run store (*db_path*, default
    ``AGENT_DB_PATH``). If staging or the commit raises or fails, the inbox
    file is removed, ``git reset`` unstages the path, no row is written and
    ``HomeGpuSubmitError`` is raised - the mailbox poll then sees no change.
    A failed push is still only reported via ``pushed=False`` (the mailbox
    poll retries unpushed commits); a committed job keeps its row.
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

    inbox = repo / "inbox"
    created_inbox = not inbox.exists()
    inbox.mkdir(exist_ok=True)
    path = inbox / f"{job_id}.md"

    committed = pushed = False
    try:
        mailbox_core._atomic_write(path, content)
        if commit:
            message = f"home-gpu: submit {kind} job [{job_id}]"
            # ``git_add_and_commit`` stages with check=True (a staging failure
            # raises) and commits with check=False (a commit failure returns
            # False); both leave the file untracked or staged, so both are
            # publication failures that must roll the file back (#812).
            if not mailbox_core.git_add_and_commit(repo, [path], message):
                raise HomeGpuSubmitError(
                    f"mailbox commit failed for job file {path.name}; "
                    "the file and any staging are rolled back and no job is queued"
                )
            committed = True
        # The queued row is written only after the inbox file is committed
        # (with commit=False: after it is written); a publication failure
        # above therefore leaves no row behind.
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
                input_sha256=digest,
            )
        finally:
            conn.close()
        if commit:
            pushed = mailbox_core.git_push(repo)
    except Exception as exc:
        if not committed:
            _rollback_publication(repo, path, remove_inbox=created_inbox)
        if isinstance(exc, HomeGpuSubmitError):
            raise
        detail = mailbox_core.redact(str(exc))[:300]
        if committed:
            # The commit already succeeded; the file stays for the poll.
            raise HomeGpuSubmitError(
                f"job {job_id} is committed in the mailbox but the submit "
                f"failed afterwards: {detail}"
            ) from exc
        raise HomeGpuSubmitError(
            f"publishing job file {path.name} failed: {detail}; "
            "the inbox file, any staging and any queued row were rolled back"
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
