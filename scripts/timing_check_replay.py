"""Replay merged commits through the slow-machine check (#681/#689).

Usage::

    python scripts/timing_check_replay.py 67c00298 [more commits ...]
    python scripts/timing_check_replay.py --last 10 --ref main   # last 10 merges of main
    python scripts/timing_check_replay.py 67c00298 --out replay.jsonl --green <sha> ...

Each commit is checked out into a temporary detached worktree. Its diff
against its first parent selects the affected tests
(``modules.timing_check.affected_tests``), and those run through
``modules.timing_check.run_timing_check`` and, for flagged tests,
``control_check`` - the same calls verification makes. A commit older than the
drift fixture (#687) gets it appended to its ``tests/conftest.py`` from this
checkout, so old commits are checked too. Prints one JSON line per commit with
``verdict`` = what verification would conclude: ``ok``, ``failed``, or
``needs_review`` (no verdict), with ``drift_only``,
``preexisting_host_failures``, and ``host_regressions`` classifications.

A commit whose replay crashes (unknown commit, git or worktree failure) gets
a ``needs_review`` record with the error and the replay continues with the
next commit. ``--out FILE`` appends every record to FILE as soon as it is
measured, so an interrupted replay keeps what it already measured. The last
stderr line is a JSON summary: verdict counts and ``false_positives`` - the
commits with verdict ``failed`` among those passed via ``--green`` (commits
whose CI was green, so any ``failed`` there is a false positive).

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
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.timing_check import (  # noqa: E402
    ControlCheckResult,
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


def verdict(result: str, control: ControlCheckResult | tuple[list[str], list[str]] | None) -> str:
    """What verification would conclude: ``ok``, ``failed`` or ``needs_review``."""
    if result == "ok":
        return "ok"
    drift_only = (
        control.drift_only if isinstance(control, ControlCheckResult) else control[0]
        if control is not None else []
    )
    host_regressions = (
        control.host_regressions if isinstance(control, ControlCheckResult) else []
    )
    if drift_only or host_regressions:
        return "failed"
    return "needs_review"


def is_flagged(record: dict) -> bool:
    return record.get("verdict", "needs_review") != "ok"


def crash_record(commit: str, exc: BaseException) -> dict:
    """The record of a commit whose replay crashed: no verdict, fail closed."""
    return {
        "commit": commit,
        "result": f"error: replay crashed: {type(exc).__name__}: {str(exc)[:300]}",
        "verdict": "needs_review",
    }


def _matches(commit: str, prefixes: Iterable[str]) -> bool:
    return any(commit.startswith(p[:12]) or p.startswith(commit) for p in prefixes)


def summarize(records: list[dict], green: Iterable[str] = ()) -> dict:
    """Verdict counts; ``false_positives`` = ``failed`` among the green commits."""
    green = [g for g in green if g]
    counts = {"ok": 0, "failed": 0, "needs_review": 0}
    for record in records:
        key = record.get("verdict", "needs_review")
        counts[key if key in counts else "needs_review"] += 1
    green_records = [r for r in records if _matches(str(r.get("commit", "")), green)]
    return {
        "commits": len(records),
        **counts,
        "green_commits": len(green_records),
        "false_positives": sum(r.get("verdict") == "failed" for r in green_records),
        "green_needs_review": sum(
            r.get("verdict", "needs_review") == "needs_review" for r in green_records
        ),
    }


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
            control = control_check(
                worktree, flagged, on_process=on_process, base_commit=f"{sha}^",
            )
            if timed_out.is_set():
                record["result"] = result = f"error: timed out after {timeout:g}s"
                control = None
            elif control is None:
                record["result"] = "error: control run without drift gave no verdict"
            else:
                record["drift_only"] = control.drift_only
                record["preexisting_host_failures"] = control.preexisting_host_failures
                record["host_regressions"] = control.host_regressions
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
        help="also replay the last N first-parent commits of --ref",
    )
    parser.add_argument(
        "--ref", default="HEAD",
        help="branch whose merges --last walks (default %(default)s; use main for merges)",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help="seconds per commit before its pytest run is killed (default %(default)s)",
    )
    parser.add_argument(
        "--out", default=None,
        help="append every JSON record to this file as soon as it is measured",
    )
    parser.add_argument(
        "--green", nargs="*", default=[], metavar="SHA",
        help="commits known green on CI; a 'failed' verdict there counts as a false positive",
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
        commits += _git("log", "--first-parent", f"-{args.last}", "--format=%H", args.ref).split()
    if not commits:
        parser.error("give at least one commit or --last N")
    exit_code = EXIT_OK

    def terminate(_signum, _frame):
        # Unwind like Ctrl-C, so the pytest group and the worktree are removed.
        raise KeyboardInterrupt

    records: list[dict] = []
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        for commit in commits:
            try:
                record = check_fn(commit, timeout=args.timeout, drift_ms=args.drift_ms)
            except Exception as exc:  # noqa: BLE001 - one broken commit is no verdict
                record = crash_record(commit, exc)
            records.append(record)
            line = json.dumps(record)
            print(line, flush=True)
            if args.out:
                with open(args.out, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            if is_flagged(record):
                exit_code = EXIT_FLAGGED
    except KeyboardInterrupt:
        exit_code = EXIT_INTERRUPTED
    finally:
        signal.signal(signal.SIGTERM, previous)
        summary = summarize(records, args.green)
        summary["interrupted"] = exit_code == EXIT_INTERRUPTED
        print(json.dumps({"summary": summary}), file=sys.stderr, flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
