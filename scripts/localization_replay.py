#!/usr/bin/env python3
"""Replay local file localization against recent merged issue commits (#696)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.llama_cpp_client import LlamaCppClient
from modules.local_file_localizer import localize
from modules.run_store import (
    DEFAULT_DB_PATH,
    init_db,
    list_local_localizations,
    upsert_local_localization,
)

_LINKED_ISSUE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE,
)
logger = logging.getLogger(__name__)


def metrics(predicted: list[str], actual: list[str], k: int = 5) -> tuple[float, float] | None:
    """Score a non-empty change set; empty sets are not replay observations."""
    predictions = list(dict.fromkeys(predicted))[:k]
    expected = set(actual)
    if not expected:
        return None
    hits = len(set(predictions) & expected)
    return hits / len(expected), hits / len(predictions) if predictions else 0.0


def store_localization(*, commit_sha: str, issue_number: int | None, predicted: list[str],
                       actual: list[str], seconds: float,
                       db_path: str = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    scores = metrics(predicted, actual)
    if scores is None:
        logger.info("localization replay skipped %s: no non-test changes", commit_sha)
        return None
    recall, precision = scores
    conn = init_db(db_path)
    try:
        upsert_local_localization(
            conn, created_at=datetime.now(timezone.utc).isoformat(), commit_sha=commit_sha,
            issue_number=issue_number, predicted=predicted, actual=actual,
            recall_at_5=recall, precision_at_5=precision, seconds=seconds,
        )
    finally:
        conn.close()
    return {"commit_sha": commit_sha, "issue_number": issue_number, "predicted": predicted,
            "actual": actual, "recall_at_5": recall, "precision_at_5": precision,
            "seconds": seconds}


def list_localizations(limit: int = 20, *, db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    conn = init_db(db_path)
    try:
        return list_local_localizations(conn, limit)
    finally:
        conn.close()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def _issue(repo: Path, number: int) -> dict[str, Any] | None:
    """Resolve an issue through gh; pull requests and lookup failures are not issues."""
    try:
        raw = subprocess.run(
            ["gh", "issue", "view", str(number), "--json", "number,title,body"],
            cwd=repo, check=True, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
        value = json.loads(raw)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("number") else None


def candidate_commits(repo: Path, *, issue_fetcher: Any = None) -> Iterator[tuple[str, str, int | None]]:
    """Yield first-parent commits in replay order, with linked issue text where available.

    Lazy on purpose: the replay pulls candidates until N commits are scored,
    so issues are only resolved for commits that are actually replayed.
    """
    raw = _git(repo, "log", "--first-parent", "--format=%H%x00%B%x00%x1e")
    fetch = issue_fetcher or (lambda number: _issue(repo, number))
    for record in raw.split("\x1e"):
        parts = record.strip().split("\x00", 1)
        if len(parts) != 2:
            continue
        message = parts[1].strip("\x00 \r\n\t")
        match = _LINKED_ISSUE_RE.search(message)
        number = int(match.group(1)) if match else None
        try:
            issue = fetch(number) if number is not None else None
        except Exception as exc:  # one bad lookup must not stop the replay
            logger.warning("localization replay: issue #%s lookup failed: %s", number, exc)
            issue = None
        if issue:
            text = f"{issue.get('title') or ''}\n\n{issue.get('body') or ''}".strip()
            yield parts[0], text, number
        else:
            # A squash suffix such as ``(#123)`` identifies the PR, not an issue.
            yield parts[0], message, None


async def replay_commit(commit_sha: str, issue_text: str, repo: str | Path, client: Any, *,
                        issue_number: int | None = None,
                        db_path: str = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    repo = Path(repo).resolve()
    actual = [p for p in _git(repo, "diff", "--name-only", f"{commit_sha}^", commit_sha).splitlines()
              if p and not (p == "tests" or p.startswith("tests/"))]
    if not actual:
        logger.info("localization replay skipped %s: no non-test changes", commit_sha)
        return None
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="nexus-localize-") as tmp:
        checkout = Path(tmp) / "repo"
        subprocess.run(["git", "worktree", "add", "--detach", str(checkout), f"{commit_sha}^"],
                       cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            predicted = await localize(issue_text, checkout, client, k=5)
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(checkout)], cwd=repo,
                           check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return store_localization(commit_sha=commit_sha, issue_number=issue_number,
                              predicted=predicted, actual=actual,
                              seconds=time.monotonic() - started, db_path=db_path)


async def replay(repo: str | Path, *, limit: int = 20, client: Any | None = None,
                 db_path: str = DEFAULT_DB_PATH, issue_fetcher: Any = None) -> list[dict[str, Any]]:
    root = Path(repo).resolve()
    llama = client or LlamaCppClient()
    rows: list[dict[str, Any]] = []
    for sha, text, number in candidate_commits(root, issue_fetcher=issue_fetcher):
        if len(rows) >= max(0, limit):
            break
        try:
            row = await replay_commit(
                sha, text, root, llama, issue_number=number, db_path=db_path,
            )
        except Exception as exc:
            logger.warning("localization replay skipped %s: %s", sha, exc)
            continue
        if row is not None:
            rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    rows = asyncio.run(replay(args.repo, limit=args.limit, db_path=args.db))
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
