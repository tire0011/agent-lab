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


def preflight(issue_text: str, repo: str | Path, budget_s: float = 300) -> PreflightResult:
    """Run issue-relevant tests on detached ``main``; all uncertainty fails open."""

    started = time.monotonic()
    try:
        budget = float(budget_s)
        if budget <= 0:
            raise TimeoutError("preflight budget exhausted")
        root = Path(repo).resolve()
        with tempfile.TemporaryDirectory(prefix="nexus-gate-preflight-") as temporary:
            worktree = Path(temporary) / "main"
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(worktree), "main"],
                cwd=root,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(0.001, budget - (time.monotonic() - started)),
            )
            try:
                tests = _selection(issue_text, worktree)
                remaining = budget - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("preflight budget exhausted")
                if not tests:
                    return PreflightResult("passed", seconds=time.monotonic() - started)

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
                elapsed = time.monotonic() - started
                if timed_out.is_set() or elapsed > budget or result == "error":
                    return PreflightResult("skipped", seconds=elapsed)
                failing = sorted(set(timing_check.flagged_tests(result)))
                if failing:
                    return PreflightResult("blocked", failing, tails, elapsed)
                if result == "ok":
                    return PreflightResult("passed", seconds=elapsed)
                return PreflightResult("skipped", seconds=elapsed)
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=root,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
    except BaseException:
        # This is deliberately broader than Exception: a gate that cannot give
        # a deterministic verdict must never consume or suppress the paid run.
        return PreflightResult("skipped", seconds=time.monotonic() - started)
