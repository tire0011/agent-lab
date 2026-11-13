"""Replay merged commits through the slow-machine check (#681).

Usage::

    python scripts/timing_check_replay.py 67c00298 [more commits ...]
    python scripts/timing_check_replay.py --last 10      # last 10 first-parent commits of HEAD

Each commit is checked out into a temporary detached worktree. Its change
against its first parent selects the affected test files, and those run
through ``modules.timing_check.run_timing_check`` exactly as in verification.
The replay uses the commit's own ``tests/conftest.py``, so a commit older than
#681 gets the drift plugin copied in from this checkout. Prints one JSON line
per commit.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules import timing_check  # noqa: E402

_PLUGIN_MARKER = "def _install_clock_drift"


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout


def _ensure_plugin(worktree: Path) -> None:
    conftest = worktree / "tests" / "conftest.py"
    current = conftest.read_text(encoding="utf-8") if conftest.exists() else ""
    if _PLUGIN_MARKER in current:
        return
    source = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    plugin = source[source.index("# Slow-machine simulation (#681)"):]
    conftest.write_text(current + "\n\n" + plugin, encoding="utf-8")


def replay(commit: str, *, timeout: float, max_files: int, drift_ms: float) -> dict:
    sha = _git("rev-parse", commit).strip()
    changed = [line for line in _git(
        "diff", "--name-only", "--no-renames", "--diff-filter=d", f"{sha}^", sha,
    ).splitlines() if line]
    parent = Path(tempfile.mkdtemp(prefix="nexus-timing-replay-"))
    worktree = parent / "worktree"
    try:
        _git("worktree", "add", "--detach", str(worktree), sha)
        _ensure_plugin(worktree)
        affected = timing_check.select_affected_test_files(changed, worktree, max_files=max_files)
        started = time.monotonic()
        result = timing_check.run_timing_check(
            sys.executable, str(worktree), affected.files, timeout_seconds=timeout, drift_ms=drift_ms,
        )
        return {
            "commit": sha[:12],
            "subject": _git("log", "-1", "--format=%s", sha).strip()[:80],
            "drift_ms": drift_ms,
            "status": result.status,
            "timing_sensitive": result.timing_sensitive,
            "test_files": affected.files,
            "dropped_test_files": affected.dropped,
            "detail": result.detail,
            "seconds": round(time.monotonic() - started, 1),
            "output_tail": result.output[-1500:] if result.status != "passed" else "",
        }
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                       cwd=ROOT, capture_output=True)
        shutil.rmtree(parent, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=ROOT, capture_output=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("commits", nargs="*")
    parser.add_argument("--last", type=int, default=0,
                        help="also replay the last N first-parent commits of HEAD")
    parser.add_argument("--timeout", type=float, default=timing_check.DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--drift-ms", type=float, default=timing_check.drift_from_env())
    parser.add_argument("--max-files", type=int, default=timing_check.DEFAULT_MAX_FILES)
    args = parser.parse_args(argv)
    commits = list(args.commits)
    if args.last:
        commits += _git("log", "--first-parent", f"-{args.last}", "--format=%H").split()
    for commit in commits:
        print(json.dumps(replay(commit, timeout=args.timeout, max_files=args.max_files,
                                 drift_ms=args.drift_ms)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
