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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping

from modules.verification_env import build_verification_test_env


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
MAX_FAILURE_TAIL_LINES = 40


class TimingCheckResult(str):
    """A string-compatible verdict with bounded per-test diagnostics."""

    failure_tails: dict[str, list[str]]

    def __new__(
        cls, value: str, failure_tails: dict[str, list[str]] | None = None,
    ) -> "TimingCheckResult":
        result = super().__new__(cls, value)
        result.failure_tails = failure_tails or {}
        return result


def _failed_test_tails(output: str, failed: Iterable[str]) -> dict[str, list[str]]:
    lines = output.splitlines()
    tails: dict[str, list[str]] = {}
    for test_id in failed:
        occurrences = [index for index, line in enumerate(lines) if test_id in line]
        end = occurrences[-1] + 1 if occurrences else len(lines)
        tails[test_id] = lines[max(0, end - MAX_FAILURE_TAIL_LINES):end]
    return tails


@dataclass(frozen=True, eq=False)
class ControlCheckResult:
    """Classification of tests flagged by the clock-drift run.

    Iteration retains the pre-#710 ``(drift_only, fails_without_drift)`` API.
    New callers should use the named classifications.
    """

    drift_only: list[str]
    preexisting_host_failures: list[str]
    host_regressions: list[str]
    no_drift_output_tail: str = ""

    @property
    def fails_without_drift(self) -> list[str]:
        return self.preexisting_host_failures + self.host_regressions

    def __iter__(self):
        yield self.drift_only
        yield self.fails_without_drift

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ControlCheckResult):
            return self.__dict__ == other.__dict__
        if isinstance(other, tuple) and len(other) == 2:
            return (self.drift_only, self.fails_without_drift) == other
        return False


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
    base_commit: str | None = None,
    env: Mapping[str, str] | None = None,
    results: list[TimingCheckResult] | None = None,
) -> ControlCheckResult | None:
    """Classify flagged tests without drift and, when needed, at their base.

    A test that also fails without drift is not shown to be timing-sensitive -
    it fails on this host anyway (a missing test dependency, host state), for
    instance when the normal run that passed was GitHub CI. Returns None when
    the control run gives no verdict.
    """
    tests = list(flagged)
    # Drift 0 keeps the fixture installed but adds nothing, so the clocks are real.
    output: list[str] = []
    control = run_timing_check(
        repo, tests, 0, on_process=on_process, python=python, env=env,
        on_output=output.append,
    )
    if results is not None:
        results.append(control)
    if control == "ok":
        return ControlCheckResult(tests, [], [], _output_tail(output))
    also_failing = set(flagged_tests(control))
    if not also_failing:
        return None
    drift_only = [test for test in tests if test not in also_failing]
    persistent = [test for test in tests if test in also_failing]
    if not base_commit:
        return ControlCheckResult(drift_only, [], persistent, _output_tail(output))

    root = Path(repo).resolve()
    worktree = Path(tempfile.mkdtemp(prefix="nexus-timing-base-"))
    try:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), base_commit],
            cwd=root, check=True, text=True, capture_output=True,
        )
        base_result = run_timing_check(
            worktree, persistent, 0, on_process=on_process, python=python, env=env,
        )
        if results is not None:
            results.append(base_result)
        if base_result == "ok":
            preexisting: list[str] = []
        else:
            base_failures = set(flagged_tests(base_result))
            if not base_failures:
                return None
            preexisting = [test for test in persistent if test in base_failures]
        regressions = [test for test in persistent if test not in preexisting]
        return ControlCheckResult(
            drift_only, preexisting, regressions, _output_tail(output),
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return None
    finally:
        try:
            # Run this even when `add` raised: git may have registered the
            # worktree before a checkout hook or interruption failed it.
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=root, check=False, text=True, capture_output=True,
            )
        except BaseException:
            pass
        shutil.rmtree(worktree, ignore_errors=True)
        try:
            subprocess.run(
                ["git", "worktree", "prune"], cwd=root, check=False,
                text=True, capture_output=True,
            )
        except BaseException:
            pass


def _output_tail(chunks: list[str], limit: int = 2000) -> str:
    tail = "".join(chunks)[-limit:].strip()
    # Pytest's elapsed time makes otherwise identical checks produce different
    # retry feedback. The duration has no diagnostic value for this check.
    return re.sub(r"\bin \d+(?:\.\d+)?s\b", "in <duration>", tail)


def run_timing_check(
    repo: str | os.PathLike[str],
    tests: Iterable[str],
    drift_ms: int | float = 50,
    *,
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
    python: str | None = None,
    env: Mapping[str, str] | None = None,
    on_output: Callable[[str], None] | None = None,
) -> TimingCheckResult:
    """Run every selected test under clock drift and summarize the outcome.

    *python* is the interpreter for the pytest child (default: this one), so
    verification can use the same interpreter as its normal test run.
    """

    test_list = list(tests)
    if not test_list:
        return TimingCheckResult("ok")
    child_env = dict(env) if env is not None else build_verification_test_env()
    child_env["NEXUS_TEST_CLOCK_DRIFT_MS"] = str(drift_ms)
    child_env["PYTHONUNBUFFERED"] = "1"
    proc: subprocess.Popen[str] | None = None
    output = ""
    result = "error"
    try:
        proc = subprocess.Popen(
            [python or sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short", "-rf", *test_list],
            cwd=Path(repo),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        if on_process is not None:
            on_process(proc)
        output, _ = proc.communicate()
        if on_output is not None:
            on_output(output or "")
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
    failed = list(dict.fromkeys(_FAILED_TEST.findall(output or ""))) if proc is not None else []
    return TimingCheckResult(result, _failed_test_tails(output or "", failed))
