"""Find and rerun tests which may depend on changed timing code."""

from __future__ import annotations

import ast
import os
import re
import signal
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from modules.verification_env import build_verification_test_env


_DIFF_PATH = re.compile(r"^(?:diff --git a/(\S+) b/(\S+)|\+\+\+ b/(\S+))$", re.MULTILINE)
_FAILED_TEST = re.compile(r"^FAILED\s+(.+?)(?:\s+-\s+.*)?$", re.MULTILINE)
_FAILURE_HEADER = re.compile(r"^_{2,}\s+(.+?)\s+_{2,}$")
_SHORT_SUMMARY = re.compile(r"^={2,}\s+short test summary info\s+={2,}$")
_MAX_FAILURE_TAIL_LINES = 40


class TimingCheckResult(str):
    """String-compatible timing verdict with bounded pytest diagnostics."""

    failure_tails: dict[str, list[str]]

    def __new__(cls, value: str, failure_tails: dict[str, list[str]] | None = None):
        result = super().__new__(cls, value)
        result.failure_tails = failure_tails or {}
        return result


def _failure_tails(output: str, failed: Iterable[str]) -> dict[str, list[str]]:
    """Return the last 40 lines of each failed test's pytest failure section."""
    lines = output.splitlines()
    summary_at = next((i for i, line in enumerate(lines) if _SHORT_SUMMARY.match(line)), len(lines))
    headers = [i for i, line in enumerate(lines[:summary_at]) if _FAILURE_HEADER.match(line)]
    tails: dict[str, list[str]] = {}
    for test_id in failed:
        test_name = test_id.rsplit("::", 1)[-1].split("[", 1)[0]
        start = next(
            (i for i in headers if test_name in (_FAILURE_HEADER.match(lines[i]).group(1))),
            None,
        )
        if start is None:
            # Collection failures and unusual plugins may not emit a normal
            # failure header. Keep bounded output rather than losing the cause.
            section = lines[:summary_at]
        else:
            end = next((i for i in headers if i > start), summary_at)
            section = lines[start:end]
        tails[test_id] = section[-_MAX_FAILURE_TAIL_LINES:]
    return tails


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


def flagged_tests(result: str) -> list[str]:
    """The test ids of a ``timing_sensitive: ...`` result, else []."""
    if not result.startswith(TIMING_SENSITIVE_PREFIX):
        return []
    return [test for test in result[len(TIMING_SENSITIVE_PREFIX):].split(", ") if test]


def control_check(
    repo: str | os.PathLike[str],
    flagged: Iterable[str],
    *,
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
    python: str | None = None,
) -> tuple[list[str], list[str]] | None:
    """Rerun flagged tests without drift: ``(drift_only, fails_without_drift)``.

    A test that also fails without drift is not shown to be timing-sensitive -
    it fails on this host anyway (a missing test dependency, host state), for
    instance when the normal run that passed was GitHub CI. Returns None when
    the control run gives no verdict.
    """
    tests = list(flagged)
    # Drift 0 keeps the fixture installed but adds nothing, so the clocks are real.
    control = run_timing_check(repo, tests, 0, on_process=on_process, python=python)
    if control == "ok":
        return tests, []
    also_failing = set(flagged_tests(control))
    if not also_failing:
        return None
    return (
        [test for test in tests if test not in also_failing],
        [test for test in tests if test in also_failing],
    )


def run_timing_check(
    repo: str | os.PathLike[str],
    tests: Iterable[str],
    drift_ms: int | float = 50,
    *,
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
    python: str | None = None,
) -> str:
    """Run every selected test under clock drift and summarize the outcome.

    *python* is the interpreter for the pytest child (default: this one), so
    verification can use the same interpreter as its normal test run.
    """

    test_list = list(tests)
    if not test_list:
        return "ok"
    env = build_verification_test_env()
    env["NEXUS_TEST_CLOCK_DRIFT_MS"] = str(drift_ms)
    env["PYTHONUNBUFFERED"] = "1"
    proc: subprocess.Popen[str] | None = None
    result = "error"
    try:
        proc = subprocess.Popen(
            [
                python or sys.executable, "-m", "pytest", "-q", "-p",
                "no:cacheprovider", "--tb=short", "-rf", *test_list,
            ],
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
            result = TimingCheckResult("ok")
        elif proc.returncode == 1:
            failed = list(dict.fromkeys(_FAILED_TEST.findall(output or "")))
            value = f"{TIMING_SENSITIVE_PREFIX}{', '.join(failed)}" if failed else "error"
            result = TimingCheckResult(value, _failure_tails(output or "", failed))
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
    return result
