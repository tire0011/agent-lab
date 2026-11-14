"""Replay merged commits through the slow-machine check (#681/#689).

Usage::

    python scripts/timing_check_replay.py 67c00298 [more commits ...]
    python scripts/timing_check_replay.py --last 10   # last 10 first-parent commits of HEAD

Each commit is checked out into a temporary detached worktree. Its diff
against its first parent selects the affected tests
(``modules.timing_check.affected_tests``), and those run through
``modules.timing_check.run_timing_check`` and, for flagged tests,
``control_check`` - the same calls verification makes. A commit older than the
drift fixture (#687) gets it appended to its ``tests/conftest.py`` from this
checkout, so old commits are checked too. Prints one JSON line per commit with
``verdict`` = what verification would conclude: ``ok``, ``failed`` (a test
fails only under drift: ``drift_only``) or ``needs_review`` (no verdict, or
the flagged tests fail on this host without drift too: ``fails_without_drift``).

Exit code: 0 when every commit was ``ok``, 1 otherwise (fail closed, as in
verification), 130 when interrupted (Ctrl-C or SIGTERM). The temporary
worktree is removed in every case.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.timing_check import (  # noqa: E402
    affected_tests,
    control_check,
    flagged_tests,
    run_timing_check,
)

EXIT_OK = 0
EXIT_FLAGGED = 1
EXIT_INTERRUPTED = 130

DEFAULT_DRIFT_MS = 50
DEFAULT_TIMEOUT_SECONDS = 900.0

# The drift fixture in tests/conftest.py (#687) spans these two markers.
_PLUGIN_START = "# Capture these before any test can replace them."
_PLUGIN_END = "# Keep the suite out of the production log directory."
_PLUGIN_PRESENT = "NEXUS_TEST_CLOCK_DRIFT_MS"
_PLUGIN_IMPORTS = "import os\nimport time\nfrom types import SimpleNamespace\nimport pytest\n\n"


def verdict(result: str, control: tuple[list[str], list[str]] | None) -> str:
    """What verification would conclude: ``ok``, ``failed`` or ``needs_review``."""
    if result == "ok":
        return "ok"
    if control is not None and control[0]:
        return "failed"
    return "needs_review"


def is_flagged(record: dict) -> bool:
    return record.get("verdict", "needs_review") != "ok"


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True,
    ).stdout


def drift_plugin(source_root: Path = ROOT) -> str:
    """The drift fixture block of this checkout's ``tests/conftest.py``."""
    source = (source_root / "tests" / "conftest.py").read_text(encoding="utf-8")
    start, end = source.index(_PLUGIN_START), source.index(_PLUGIN_END)
    return source[start:end]


def ensure_plugin(worktree: Path, source_root: Path = ROOT) -> bool:
    """Append the drift fixture to an older commit's conftest; True if added."""
    conftest = worktree / "tests" / "conftest.py"
    current = conftest.read_text(encoding="utf-8") if conftest.exists() else ""
    if _PLUGIN_PRESENT in current:
        return False
    conftest.parent.mkdir(parents=True, exist_ok=True)
    conftest.write_text(
        current + "\n\n# Added by scripts/timing_check_replay.py\n"
        + _PLUGIN_IMPORTS + drift_plugin(source_root),
        encoding="utf-8",
    )
    return True


def _kill_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def check_commit(commit: str, *, timeout: float, drift_ms: float) -> dict:
    sha = _git("rev-parse", "--verify", f"{commit}^{{commit}}").strip()
    diff = _git("diff", "--no-renames", f"{sha}^", sha)
    parent = Path(tempfile.mkdtemp(prefix="nexus-timing-replay-"))
    worktree = parent / "worktree"
    timed_out = threading.Event()
    timers: list[threading.Timer] = []

    def on_process(process: subprocess.Popen[str] | None) -> None:
        if process is None:
            for timer in timers:
                timer.cancel()
            return

        def expire() -> None:
            timed_out.set()
            _kill_group(process)

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timers.append(timer)
        timer.start()

    try:
        _git("worktree", "add", "--detach", str(worktree), sha)
        plugin_added = ensure_plugin(worktree)
        tests = affected_tests(diff, worktree)
        started = time.monotonic()
        result = run_timing_check(worktree, tests, drift_ms, on_process=on_process)
        if timed_out.is_set():
            result = f"error: timed out after {timeout:g}s"
        record = {
            "commit": sha[:12],
            "subject": _git("log", "-1", "--format=%s", sha).strip()[:80],
            "drift_ms": drift_ms,
            "plugin_added": plugin_added,
            "tests": tests,
            "result": result,
        }
        control = None
        flagged = flagged_tests(result)
        if flagged:
            # A replay has no normal run, so this is its only control.
            control = control_check(worktree, flagged, on_process=on_process)
            if timed_out.is_set():
                record["result"] = result = f"error: timed out after {timeout:g}s"
                control = None
            elif control is None:
                record["result"] = "error: control run without drift gave no verdict"
            else:
                record["drift_only"], record["fails_without_drift"] = control
        record["verdict"] = verdict(record["result"], control)
        record["seconds"] = round(time.monotonic() - started, 1)
        return record
    finally:
        for timer in timers:
            timer.cancel()
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=ROOT, capture_output=True,
        )
        shutil.rmtree(parent, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=ROOT, capture_output=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit codes: 0 all ok, 1 a commit was flagged or errored, 130 interrupted",
    )
    parser.add_argument("commits", nargs="*", help="commits to replay")
    parser.add_argument(
        "--last", type=int, default=0,
        help="also replay the last N first-parent commits of HEAD",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help="seconds per commit before its pytest run is killed (default %(default)s)",
    )
    parser.add_argument(
        "--drift-ms", type=float, default=DEFAULT_DRIFT_MS,
        help="clock drift per clock read in ms (default %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None, *, check_fn=check_commit) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    commits = list(args.commits)
    if args.last:
        commits += _git("log", "--first-parent", f"-{args.last}", "--format=%H").split()
    if not commits:
        parser.error("give at least one commit or --last N")
    exit_code = EXIT_OK

    def terminate(_signum, _frame):
        # Unwind like Ctrl-C, so the pytest group and the worktree are removed.
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        for commit in commits:
            record = check_fn(commit, timeout=args.timeout, drift_ms=args.drift_ms)
            print(json.dumps(record), flush=True)
            if is_flagged(record):
                exit_code = EXIT_FLAGGED
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    finally:
        signal.signal(signal.SIGTERM, previous)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
