"""Fail-open test preflight before Nexus spends a paid coding start."""

from __future__ import annotations

import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from modules import timing_check


MAX_TESTS = 40
_PATH = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*[\w.-]+\.py)(?![\w./-])")


@dataclass(frozen=True, slots=True)
class PreflightResult:
    status: str
    failing_ids: list[str] = field(default_factory=list)
    tails: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0
    # The main commit the verdict is for; a blocked issue is retried only
    # once main has moved past it (or the issue text changed).
    main_sha: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"passed", "blocked", "skipped"}:
            raise ValueError(f"invalid preflight status: {self.status}")


def _mentioned_paths(issue_text: str) -> tuple[list[str], list[str]]:
    tests: list[str] = []
    modules: list[str] = []
    for raw in _PATH.findall(issue_text or ""):
        path = PurePosixPath(raw).as_posix()
        if path.startswith("../") or path.startswith("/"):
            continue
        name = PurePosixPath(path).name
        if name.startswith("test_") or name.endswith("_test.py"):
            tests.append(path)
        else:
            modules.append(path)
    return list(dict.fromkeys(tests)), list(dict.fromkeys(modules))


def _selection(issue_text: str, worktree: Path) -> list[str]:
    named_tests, modules = _mentioned_paths(issue_text)
    selected = [path for path in named_tests if (worktree / path).is_file()]
    if modules:
        # affected_tests consumes only paths from a diff; no repository contents
        # are changed by this synthetic diff.
        diff = "".join(f"diff --git a/{path} b/{path}\n" for path in modules)
        selected.extend(timing_check.affected_tests(diff, worktree))
    return list(dict.fromkeys(selected))[:MAX_TESTS]


def main_sha(repo: str | Path, timeout: float = 10) -> str | None:
    """The commit local ``main`` points at; ``None`` when it cannot be read."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--verify", "main^{commit}"],
            cwd=Path(repo), capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and sha else None


def preflight(issue_text: str, repo: str | Path, budget_s: float = 300) -> PreflightResult:
    """Run issue-relevant tests on detached ``main``; all uncertainty fails open.

    The whole call, worktree cleanup included, stays within *budget_s*: a
    verdict reached only after the budget ran out is reported as ``skipped``.
    """

    started = time.monotonic()
    try:
        budget = float(budget_s)
        if budget <= 0:
            raise TimeoutError("preflight budget exhausted")
        root = Path(repo).resolve()
        sha = main_sha(root, timeout=max(0.001, budget))
        if sha is None:
            raise RuntimeError("main is not readable")
        with tempfile.TemporaryDirectory(prefix="nexus-gate-preflight-") as temporary:
            worktree = Path(temporary) / "main"
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(worktree), sha],
                cwd=root,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(0.001, budget - (time.monotonic() - started)),
            )
            verdict: PreflightResult
            try:
                verdict = _run(issue_text, worktree, started, budget)
            finally:
                # Bounded by what is left of the budget, so cleanup can never
                # hang the tick; a leftover worktree is pruned by git later.
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=root,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=max(1.0, budget - (time.monotonic() - started)),
                )
        elapsed = time.monotonic() - started
        if elapsed > budget:
            return PreflightResult("skipped", seconds=elapsed, main_sha=sha)
        return PreflightResult(
            verdict.status, verdict.failing_ids, verdict.tails, elapsed, main_sha=sha,
        )
    except BaseException:
        # This is deliberately broader than Exception: a gate that cannot give
        # a deterministic verdict must never consume or suppress the paid run.
        return PreflightResult("skipped", seconds=time.monotonic() - started)


def _run(issue_text: str, worktree: Path, started: float, budget: float) -> PreflightResult:
    """Select and run the issue's tests in *worktree*; the caller cleans up."""
    tests = _selection(issue_text, worktree)
    remaining = budget - (time.monotonic() - started)
    if remaining <= 0:
        raise TimeoutError("preflight budget exhausted")
    if not tests:
        return PreflightResult("passed")

    tails: dict[str, str] = {}
    timer: threading.Timer | None = None
    timed_out = threading.Event()

    def observe(process: subprocess.Popen[str] | None) -> None:
        nonlocal timer
        if process is None:
            if timer is not None:
                timer.cancel()
            return

        def expire() -> None:
            timed_out.set()
            timing_check._stop(process)

        timer = threading.Timer(remaining, expire)
        timer.daemon = True
        timer.start()

    result = timing_check.run_timing_check(
        worktree, tests, drift_ms=0, on_process=observe, tails=tails
    )
    if timed_out.is_set() or result == "error":
        return PreflightResult("skipped")
    failing = sorted(set(timing_check.flagged_tests(result)))
    if failing:
        return PreflightResult("blocked", failing, tails)
    if result == "ok":
        return PreflightResult("passed")
    return PreflightResult("skipped")
