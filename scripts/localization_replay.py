#!/usr/bin/env python3
"""Replay local file localization against recent merged issue commits (#696)."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

_ISSUE_RE = re.compile(r"#(\d+)")


def metrics(predicted: list[str], actual: list[str], k: int = 5) -> tuple[float, float]:
    predictions = list(dict.fromkeys(predicted))[:k]
    expected = set(actual)
    hits = len(set(predictions) & expected)
    return (hits / len(expected) if expected else 1.0,
            hits / len(predictions) if predictions else (1.0 if not expected else 0.0))


def store_localization(*, commit_sha: str, issue_number: int | None, predicted: list[str],
                       actual: list[str], seconds: float, db_path: str = DEFAULT_DB_PATH) -> dict[str, Any]:
    recall, precision = metrics(predicted, actual)
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


def candidate_commits(repo: Path, limit: int = 20) -> list[tuple[str, str, int]]:
    raw = _git(repo, "log", "--first-parent", "--format=%H%x00%B%x00%x1e", "-n", str(limit * 8))
    result: list[tuple[str, str, int]] = []
    for record in raw.split("\x1e"):
        parts = record.strip().split("\x00", 1)
        if len(parts) != 2:
            continue
        match = _ISSUE_RE.search(parts[1])
        if match:
            result.append((parts[0], parts[1].strip(), int(match.group(1))))
        if len(result) >= limit:
            break
    return result


async def replay_commit(commit_sha: str, issue_text: str, repo: str | Path, client: Any, *,
                        issue_number: int | None = None, db_path: str = DEFAULT_DB_PATH) -> dict[str, Any]:
    repo = Path(repo).resolve()
    actual = [p for p in _git(repo, "diff", "--name-only", f"{commit_sha}^", commit_sha).splitlines()
              if p and not (p == "tests" or p.startswith("tests/"))]
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
                 db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    root = Path(repo).resolve()
    llama = client or LlamaCppClient()
    return [await replay_commit(sha, text, root, llama, issue_number=number, db_path=db_path)
            for sha, text, number in candidate_commits(root, limit)]


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
