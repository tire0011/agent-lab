"""Replay merged commits through the slow-machine check (#681).

Usage::

    python scripts/timing_check_replay.py 67c00298 [more commits ...]
    python scripts/timing_check_replay.py --last 10      # last 10 first-parent commits of HEAD

Each commit is checked out into a temporary detached worktree. Its change
against its first parent selects the affected test files, and those run
through ``modules.timing_check.run_timing_check`` exactly as in verification.
Every affected test file runs; there is no file cap. The replay uses the
commit's own ``tests/conftest.py``, so a commit older than #681 gets the drift
plugin copied in from this checkout. Prints one JSON line per commit.

Exit code: 0 when every commit passed (or touched no test), 1 when any commit
was ``timing_sensitive`` or ``inconclusive`` (fail closed, as in
verification), 130 when interrupted. The temporary worktree is removed in
every case.
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
_PLUGIN_START = "# Slow-machine simulation (#681)"

EXIT_OK = 0
EXIT_FLAGGED = 1
EXIT_INTERRUPTED = 130


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout


def _ensure_plugin(worktree: Path, source_root: Path = ROOT) -> None:
    """Append the drift plugin to an older commit's conftest; idempotent."""
    conftest = worktree / "tests" / "conftest.py"
    current = conftest.read_text(encoding="utf-8") if conftest.exists() else ""
    if _PLUGIN_MARKER in current:
        return
    source = (source_root / "tests" / "conftest.py").read_text(encoding="utf-8")
    plugin = source[source.index(_PLUGIN_START):]
    conftest.parent.mkdir(parents=True, exist_ok=True)
    conftest.write_text(current + "\n\n" + plugin, encoding="utf-8")


def replay(commit: str, *, timeout: float, drift_ms: float) -> dict:
    sha = _git("rev-parse", commit).strip()
    changed = [line for line in _git(
        "diff", "--name-only", "--no-renames", "--diff-filter=d", f"{sha}^", sha,
    ).splitlines() if line]
    parent = Path(tempfile.mkdtemp(prefix="nexus-timing-replay-"))
    worktree = parent / "worktree"
    try:
        _git("worktree", "add", "--detach", str(worktree), sha)
        _ensure_plugin(worktree)
        affected = timing_check.select_affected_test_files(changed, worktree)
        started = time.monotonic()
        if affected.discovery_errors:
            result = timing_check.TimingCheckResult(
                timing_check.STATUS_INCONCLUSIVE, test_files=affected.files, drift_ms=drift_ms,
                detail="affected-test discovery incomplete: "
                + "; ".join(affected.discovery_errors[:10]),
            )
        else:
            result = timing_check.run_timing_check(
                sys.executable, str(worktree), affected.files,
                timeout_seconds=timeout, drift_ms=drift_ms,
            )
        return {
            "commit": sha[:12],
            "subject": _git("log", "-1", "--format=%s", sha).strip()[:80],
            "drift_ms": drift_ms,
            "status": result.status,
            "timing_sensitive": result.timing_sensitive,
            "test_files": affected.files,
            "detail": result.detail,
            "seconds": round(time.monotonic() - started, 1),
            "output_tail": result.output[-1500:] if result.status != "passed" else "",
        }
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                       cwd=ROOT, capture_output=True)
        shutil.rmtree(parent, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=ROOT, capture_output=True)


def _interrupted(record: dict) -> bool:
    return (record.get("status") == timing_check.STATUS_INCONCLUSIVE
            and str(record.get("detail", "")).startswith(timing_check.INTERRUPTED_DETAIL))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("commits", nargs="*")
    parser.add_argument("--last", type=int, default=0,
                        help="also replay the last N first-parent commits of HEAD")
    parser.add_argument("--timeout", type=float, default=timing_check.DEFAULT_TIMEOUT_SECONDS,
                        help="seconds per commit (default %(default)s)")
    parser.add_argument("--drift-ms", type=float, default=timing_check.drift_from_env(),
                        help="clock drift per read in ms (default %(default)s)")
    return parser


def main(argv: list[str] | None = None, *, replay_fn=replay) -> int:
    args = build_parser().parse_args(argv)
    commits = list(args.commits)
    if args.last:
        commits += _git("log", "--first-parent", f"-{args.last}", "--format=%H").split()
    exit_code = EXIT_OK
    try:
        for commit in commits:
            record = replay_fn(commit, timeout=args.timeout, drift_ms=args.drift_ms)
            print(json.dumps(record), flush=True)
            if _interrupted(record):
                return EXIT_INTERRUPTED
            if record.get("status") in (timing_check.STATUS_TIMING_SENSITIVE,
                                        timing_check.STATUS_INCONCLUSIVE):
                exit_code = EXIT_FLAGGED
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
