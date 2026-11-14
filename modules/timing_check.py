"""Find and rerun tests which may depend on changed timing code."""

from __future__ import annotations

import ast
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable


_DIFF_PATH = re.compile(r"^(?:diff --git a/(\S+) b/(\S+)|\+\+\+ b/(\S+))$", re.MULTILINE)
_FAILED_TEST = re.compile(r"^FAILED\s+(.+?)(?:\s+-\s+.*)?$", re.MULTILINE)


def _changed_paths(diff: str) -> set[str]:
    paths: set[str] = set()
    for match in _DIFF_PATH.finditer(diff):
        paths.update(value for value in match.groups() if value)
    return paths


def _module_names(path: str) -> set[str]:
    parsed = PurePosixPath(path)
    if parsed.suffix != ".py":
        return set()
    module_path = parsed.parent if parsed.name == "__init__.py" else parsed.with_suffix("")
    dotted = ".".join(module_path.parts)
    # A package-local import is commonly written both with and without the
    # leading source directory, so recognize either spelling.
    names = {dotted}
    if parsed.parts and parsed.parts[0] in {"src", "lib"} and len(parsed.parts) > 1:
        names.add(".".join(module_path.parts[1:]))
    return names


def _imports(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _is_test(path: PurePosixPath) -> bool:
    return path.suffix == ".py" and (path.name.startswith("test_") or path.name.endswith("_test.py"))


def affected_tests(diff: str, repo: str | os.PathLike[str]) -> list[str]:
    """Return changed tests and tests which import a changed Python module."""

    root = Path(repo)
    changed = _changed_paths(diff)
    selected = {
        path for path in changed
        if _is_test(PurePosixPath(path)) and (root / path).is_file()
    }
    modules = set().union(*(_module_names(path) for path in changed)) if changed else set()
    if modules:
        for candidate in root.rglob("*.py"):
            try:
                relative = candidate.relative_to(root).as_posix()
            except ValueError:
                continue
            if not _is_test(PurePosixPath(relative)) or relative in selected:
                continue
            imports = _imports(candidate)
            if any(name == module or name.startswith(module + ".") for name in imports for module in modules):
                selected.add(relative)
    return sorted(selected)


def _stop(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                proc.kill()
            except OSError:
                pass
        proc.wait()


TIMING_SENSITIVE_PREFIX = "timing_sensitive: "


@dataclass(frozen=True)
class TimingCheckRun:
    """Machine verdict plus the bounded diagnostic output used to reach it."""

    result: str
    output: str = ""


@dataclass(frozen=True)
class ControlCheckResult:
    """Classification of drift failures after branch and base controls."""

    drift_only: list[str]
    preexisting_host_failures: list[str]
    host_regressions: list[str]
    control_failure_tails: dict[str, str] = field(default_factory=dict)


def _failure_tails(run: TimingCheckRun) -> dict[str, str]:
    """Return a bounded pytest diagnostic for every failed test in *run*."""
    failed = flagged_tests(run.result)
    tail = "\n".join(run.output.splitlines()[-40:])
    return {test: tail for test in failed}


def flagged_tests(result: str) -> list[str]:
    """The test ids of a ``timing_sensitive: ...`` result, else []."""
    if not result.startswith(TIMING_SENSITIVE_PREFIX):
        return []
    return [test for test in result[len(TIMING_SENSITIVE_PREFIX):].split(", ") if test]


def control_check(
    repo: str | os.PathLike[str],
    flagged: Iterable[str],
    *,
    base_commit: str | None = None,
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
    python: str | None = None,
) -> ControlCheckResult | None:
    """Classify flagged tests using no-drift runs on the branch and its base.

    A test that also fails without drift is not shown to be timing-sensitive -
    it fails on this host anyway (a missing test dependency, host state), for
    instance when the normal run that passed was GitHub CI. Returns None when
    the control run gives no verdict.
    """
    tests = list(flagged)
    # Drift 0 keeps the fixture installed but adds nothing, so the clocks are real.
    control = run_timing_check(
        repo, tests, 0, on_process=on_process, python=python, details=True,
    )
    if isinstance(control, str):  # Friendly to callers/tests mocking the legacy API.
        control = TimingCheckRun(control)
    if control.result == "ok":
        return ControlCheckResult(tests, [], [], {})
    also_failing = set(flagged_tests(control.result))
    if not also_failing:
        return None
    drift_only = [test for test in tests if test not in also_failing]
    tails = _failure_tails(control)
    # Kept for the replay tool and direct callers that do not have a meaningful
    # experiment base. Verification always supplies the base commit.
    if not base_commit:
        return ControlCheckResult(drift_only, [], [t for t in tests if t in also_failing], tails)

    parent = Path(tempfile.mkdtemp(prefix="nexus-timing-base-"))
    worktree = parent / "worktree"
    try:
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), base_commit],
            cwd=Path(repo), text=True, capture_output=True,
        )
        if added.returncode != 0:
            return None
        base = run_timing_check(
            worktree, [t for t in tests if t in also_failing], 0,
            on_process=on_process, python=python, details=True,
        )
        if isinstance(base, str):
            base = TimingCheckRun(base)
        if base.result == "ok":
            base_failing: set[str] = set()
        else:
            base_failing = set(flagged_tests(base.result))
            if not base_failing:
                return None
        preexisting = [test for test in tests if test in also_failing & base_failing]
        regressions = [test for test in tests if test in also_failing - base_failing]
        return ControlCheckResult(drift_only, preexisting, regressions, tails)
    finally:
        # Cleanup must also run for observer interruptions (including Ctrl-C).
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=Path(repo), text=True, capture_output=True,
        )
        shutil.rmtree(parent, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"], cwd=Path(repo),
            text=True, capture_output=True,
        )


def run_timing_check(
    repo: str | os.PathLike[str],
    tests: Iterable[str],
    drift_ms: int | float = 50,
    *,
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
    python: str | None = None,
    details: bool = False,
) -> str | TimingCheckRun:
    """Run every selected test under clock drift and summarize the outcome.

    *python* is the interpreter for the pytest child (default: this one), so
    verification can use the same interpreter as its normal test run.
    """

    test_list = list(tests)
    if not test_list:
        return TimingCheckRun("ok") if details else "ok"
    env = os.environ.copy()
    env["NEXUS_TEST_CLOCK_DRIFT_MS"] = str(drift_ms)
    proc: subprocess.Popen[str] | None = None
    result = "error"
    output = ""
    try:
        proc = subprocess.Popen(
            [python or sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", *test_list],
            cwd=Path(repo),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        if on_process is not None:
            on_process(proc)
        output, _ = proc.communicate()
        if proc.returncode == 0:
            result = "ok"
        elif proc.returncode == 1:
            failed = list(dict.fromkeys(_FAILED_TEST.findall(output or "")))
            result = f"{TIMING_SENSITIVE_PREFIX}{', '.join(failed)}" if failed else "error"
    except Exception:  # spawning, observers, and malformed process results are check errors
        result = "error"
    finally:
        if proc is not None:
            _stop(proc)
        if on_process is not None:
            try:
                on_process(None)
            except Exception:
                result = "error"
    run = TimingCheckRun(result, output or "")
    return run if details else run.result
