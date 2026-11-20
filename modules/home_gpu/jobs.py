"""Submit batch jobs for the home GPU through the shared mailbox (#759).

``submit_job`` writes ``inbox/<job_id>.md`` into the existing mailbox working
copy::

    ---
    type: batch_job
    to: home-gpu
    created_by: nexus
    id: <job_id>
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
``git_push`` from ``modules.mailbox.core``); this module has no git code.
The whole feature is off unless ``NEXUS_HOME_GPU=1``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from modules import run_store
from modules.mailbox import core as mailbox_core

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

    With *commit* the file is committed and pushed through the mailbox git
    helpers; without it the caller commits. Every check runs before anything
    is written: disabled flag, unknown kind, bad deadline and rejected payload
    all raise and leave the mailbox untouched. A failed push is reported via
    ``pushed=False`` (the mailbox poll retries unpushed commits); a failed
    commit raises.
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
    deadline = _iso(now + timedelta(seconds=deadline_s))
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    content = (
        "---\n"
        f"type: {JOB_TYPE}\n"
        f"to: {RECIPIENT}\n"
        f"created_by: {CREATED_BY}\n"
        f"id: {job_id}\n"
        f"job_id: {job_id}\n"
        f"kind: {kind}\n"
        f"deadline: {deadline}\n"
        f"input_sha256: {digest}\n"
        "---\n"
        f"{body}\n"
    )

    inbox = repo / "inbox"
    inbox.mkdir(exist_ok=True)
    path = inbox / f"{job_id}.md"
    with closing(run_store.init_db(db_path)) as db, db:
        db.execute(
            """INSERT INTO home_gpu_jobs
               (job_id, kind, status, submitted_at, deadline, input_sha256)
               VALUES (?, ?, 'queued', ?, ?, ?)""",
            (job_id, kind, _iso(now), deadline, digest),
        )
        try:
            mailbox_core._atomic_write(path, content)
            db.commit()
        except Exception:
            path.unlink(missing_ok=True)
            raise

    committed = pushed = False
    if commit:
        message = f"home-gpu: submit {kind} job [{job_id}]"
        if not mailbox_core.git_add_and_commit(repo, [path], message):
            raise HomeGpuJobError(
                f"job file {path.name} written but the mailbox commit failed"
            )
        committed = True
        pushed = mailbox_core.git_push(repo)

    return SubmittedJob(
        job_id=job_id,
        kind=kind,
        path=path,
        deadline=deadline,
        input_sha256=digest,
        committed=committed,
        pushed=pushed,
    )
