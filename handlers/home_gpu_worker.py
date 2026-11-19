"""The PC side of the home GPU lane: a mailbox poll with a ``batch_job`` handler.

Run it on the PC from a checkout of the public agent-lab repository::

    python -m handlers.home_gpu_worker --mailbox PATH --port 8080 --loop

It is the mailbox client's normal poll for the recipient ``home-gpu``:

1. commit reply files left by an interrupted earlier poll,
2. ``git pull --rebase`` the mailbox repository,
3. dispatch every ``inbox/*.md`` addressed to ``home-gpu`` by its ``type``
   (``DISPATCH``; an existing mailbox client registers the handler with
   :func:`register`),
4. commit the new ``outbox/<job_id>.reply.md`` files and push them, retrying
   the push after a rebase when the remote moved.

Every step is idempotent. A reply file that exists is never recomputed, so a
crash after the llama call but before the push only repeats the Git steps.
Git is the only process started, always with a fixed argument list and never
through a shell; nothing from a message is executed.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handlers import batch_job  # noqa: E402

logger = logging.getLogger("home_gpu_worker")
RECIPIENT = "home-gpu"
DISPATCH: dict[str, Callable[..., Path | None]] = {"batch_job": batch_job.handle}
_REPLY_NAME = re.compile(r"outbox/[0-9a-f]{32}\.reply\.md")
_LOCK_STALE_SECONDS = 2 * 3600
_PUSH_ATTEMPTS = 3


def register(dispatch: dict[str, Callable[..., Path | None]]) -> None:
    """Add the ``batch_job`` handler to an existing mailbox client dispatch table."""
    dispatch["batch_job"] = batch_job.handle


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=120)
    if check and result.returncode:
        raise RuntimeError(f"git {args[0]} failed with exit code {result.returncode}")
    return result


def _addressed_to_home_gpu(meta: dict[str, str]) -> bool:
    return meta.get("to", "").strip().lower() == RECIPIENT


class _PollLock:
    """One poll at a time per mailbox checkout; a lock older than two hours is stale."""

    def __init__(self, repo: Path) -> None:
        self.path = repo / ".git" / "home-gpu-worker.lock"
        self.held = False

    def __enter__(self) -> bool:
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime < _LOCK_STALE_SECONDS:
                        return False
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            os.close(fd)
            self.held = True
            return True
        return False

    def __exit__(self, *_exc) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)


def _pending_replies(repo: Path) -> list[str]:
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all", "--", "outbox").stdout
    return sorted({line[3:] for line in status.splitlines()
                   if line[:2] in ("??", "A ", "AM") and _REPLY_NAME.fullmatch(line[3:])})


def _commit(repo: Path, paths: list[str]) -> bool:
    if not paths:
        return False
    _git(repo, "add", "--", *paths)
    _git(repo, "commit", "-q", "-m", f"home-gpu: {len(paths)} batch job repl{'y' if len(paths) == 1 else 'ies'}",
         "--", *paths)
    return True


def _ahead(repo: Path) -> bool:
    result = _git(repo, "rev-list", "--count", "@{upstream}..HEAD", check=False)
    return result.returncode == 0 and result.stdout.strip() not in ("", "0")


def _push(repo: Path) -> bool:
    for _ in range(_PUSH_ATTEMPTS):
        if _git(repo, "push", "-q", check=False).returncode == 0:
            return True
        if _git(repo, "pull", "-q", "--rebase", check=False).returncode:
            _git(repo, "rebase", "--abort", check=False)
            return False
    return False


def poll_once(mailbox: Path, *, key: bytes, port: int = 8080, now: float | None = None,
              opener=None) -> dict:
    """One mailbox poll. Returns counts; never raises for one bad message."""
    mailbox = Path(mailbox)
    summary = {"answered": 0, "skipped": 0, "failed": 0, "pushed": False, "locked": False}
    with _PollLock(mailbox) as locked:
        if not locked:
            summary["locked"] = True
            return summary
        # An interrupted poll can leave a reply that was written but not committed.
        _commit(mailbox, _pending_replies(mailbox))
        if _git(mailbox, "pull", "-q", "--rebase", check=False).returncode:
            _git(mailbox, "rebase", "--abort", check=False)
            logger.warning("mailbox pull failed; replies stay local until the next poll")
        inbox, outbox = mailbox / "inbox", mailbox / "outbox"
        written: list[str] = []
        for path in sorted(inbox.glob("*.md")) if inbox.is_dir() and not inbox.is_symlink() else []:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * batch_job.MAX_PAYLOAD:
                continue
            try:
                meta, body = batch_job.split_message(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                continue  # not a message this recipient understands
            if not _addressed_to_home_gpu(meta):
                continue
            handler = DISPATCH.get(meta.get("type", ""))
            if handler is None:
                summary["skipped"] += 1
                logger.warning("refused %s: unsupported type", path.name)
                continue
            try:
                reply = handler(meta, body, outbox, key=key, port=port, now=now, opener=opener)
            except Exception as exc:  # noqa: BLE001 - one bad job never stops the poll
                summary["failed"] += 1
                logger.warning("job %s failed: %s", path.name, type(exc).__name__)
                continue
            if reply is None:
                summary["skipped"] += 1
                continue
            name = reply.relative_to(mailbox).as_posix()
            if name in _pending_replies(mailbox):
                written.append(name)
                summary["answered"] += 1
        _commit(mailbox, written)
        if _ahead(mailbox):
            summary["pushed"] = _push(mailbox)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mailbox", type=Path, required=True, help="mailbox repository checkout")
    parser.add_argument("--port", type=int, default=8080, help="loopback llama-server port")
    parser.add_argument("--key-file", help=f"shared HMAC key (default: ${batch_job.ENV_KEY_FILE})")
    parser.add_argument("--loop", action="store_true", help="poll until interrupted")
    parser.add_argument("--interval", type=float, default=60.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    key = batch_job.load_key(args.key_file)
    while True:
        try:
            logger.info("poll %s", poll_once(args.mailbox, key=key, port=args.port))
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            logger.warning("poll failed: %s", type(exc).__name__)
        if not args.loop:
            return 0
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
