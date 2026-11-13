"""Slow-machine check before merge (#681).

The first merge of #675 passed every test on the VM and turned CI on main red:
two tests computed a token budget from wall-clock deltas, and the CI runner is
slower than the VM, so it came out one token off. Nothing in the pipeline
looked for that kind of test.

This module runs the test files a change touches a second time with a
sped-up clock. ``tests/conftest.py`` wraps ``time.monotonic``,
``time.perf_counter`` and ``time.time`` when ``NEXUS_TEST_CLOCK_DRIFT_MS`` is
set, so every clock read moves forward by that drift on top of real time.
That is a slower machine, simulated deterministically.

A test that fails under drift is run once more without drift. Only a test
that passes there counts as ``timing_sensitive``. A failure that also happens
without drift belongs to the normal verification, not to this check. Anything
that leaves the answer open (pytest missing, collection error, timeout) is
reported as ``inconclusive`` and never fails a change.

Switch: ``NEXUS_TIMING_CHECK=1|0``, on by default; the drift per clock read is
``NEXUS_TIMING_CHECK_DRIFT_MS`` (default 10, see ``DEFAULT_DRIFT_MS``). The rerun covers only the
affected files, so it stays cheap.
"""

from __future__ import annotations

import ast
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from modules.verification_env import build_verification_test_env

TIMING_CHECK_ENV_VAR = "NEXUS_TIMING_CHECK"
CLOCK_DRIFT_ENV_VAR = "NEXUS_TEST_CLOCK_DRIFT_MS"
MAX_FILES_ENV_VAR = "NEXUS_TIMING_CHECK_MAX_FILES"
TIMEOUT_ENV_VAR = "NEXUS_TIMING_CHECK_TIMEOUT_SECONDS"
DRIFT_ENV_VAR = "NEXUS_TIMING_CHECK_DRIFT_MS"

# Measured, not guessed (#681): replaying 67c00298 at 10 ms flags exactly the
# two tests CI failed on (3776 != 3775 in test_issue_642, 359 vs 360 in
# test_issue_667) and nothing on the last 10 merges. 5 ms finds neither; from
# 20 ms on, two #667 streaming tests fail on every commit - they pace a fake
# server's real sleeps against the client clock, and a drift that large per
# clock read is a slower machine than any CI runner. The issue proposed 50.
DEFAULT_DRIFT_MS = 10.0
DEFAULT_MAX_FILES = 40
DEFAULT_TIMEOUT_SECONDS = 900.0

#: Prefix of the verification error, read by the repair and retry prompts.
TIMING_SENSITIVE_PREFIX = "timing_sensitive"

#: What the retry task is told to do about it.
TIMING_SENSITIVE_HINT = (
    "These tests pass normally but fail when the clock runs slower "
    f"({CLOCK_DRIFT_ENV_VAR}), as it does on the slower CI runner. Inject the "
    "clock into the code under test, or compute the value from configured "
    "values, not from wall time (time.monotonic/perf_counter/time deltas)."
)

_FALSE_VALUES = {"0", "false", "no", "off"}
_MAX_REPORTED_IDS = 20
_MAX_OUTPUT_CHARS = 8000

STATUS_PASSED = "passed"
STATUS_TIMING_SENSITIVE = "timing_sensitive"
STATUS_INCONCLUSIVE = "inconclusive"
STATUS_SKIPPED = "skipped"


def timing_check_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``NEXUS_TIMING_CHECK``: on unless explicitly switched off."""
    source = os.environ if env is None else env
    return (source.get(TIMING_CHECK_ENV_VAR, "1").strip().lower() not in _FALSE_VALUES)


def _positive_float(raw: str | None, default: float) -> float:
    try:
        value = float(raw or "")
    except ValueError:
        return default
    return value if 0 < value < float("inf") else default


def max_files_from_env(env: Mapping[str, str] | None = None) -> int:
    source = os.environ if env is None else env
    return int(_positive_float(source.get(MAX_FILES_ENV_VAR), DEFAULT_MAX_FILES))


def drift_from_env(env: Mapping[str, str] | None = None) -> float:
    source = os.environ if env is None else env
    return _positive_float(source.get(DRIFT_ENV_VAR), DEFAULT_DRIFT_MS)


def timeout_from_env(env: Mapping[str, str] | None = None) -> float:
    source = os.environ if env is None else env
    return _positive_float(source.get(TIMEOUT_ENV_VAR), DEFAULT_TIMEOUT_SECONDS)


# --- Which test files a change touches -------------------------------------

_DIFF_GIT_RE = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)$")


def changed_paths_from_diff(diff_text: str) -> list[str]:
    """Paths a unified ``git diff`` adds or changes; deleted files are left out."""
    paths: list[str] = []
    current: str | None = None
    deleted = False

    def flush() -> None:
        if current and not deleted and current not in paths:
            paths.append(current)

    for line in (diff_text or "").splitlines():
        match = _DIFF_GIT_RE.match(line)
        if match:
            flush()
            current, deleted = match.group("b"), False
        elif line.startswith("deleted file mode"):
            deleted = True
        elif line.startswith("+++ ") and current is not None:
            target = line[4:].strip()
            if target == "/dev/null":
                deleted = True
            elif target.startswith("b/"):
                current = target[2:]
    flush()
    return paths


def module_name_for_path(path: str) -> str | None:
    """Dotted import name of a repository ``.py`` file, or None."""
    if not path.endswith(".py"):
        return None
    parts = Path(path).with_suffix("").parts
    if not parts or any(not part.isidentifier() for part in parts[:-1]):
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not parts[-1].isidentifier():
        return None
    return ".".join(parts)


def _is_test_file(path: str) -> bool:
    candidate = Path(path)
    return (
        bool(candidate.parts) and candidate.parts[0] == "tests"
        and candidate.suffix == ".py" and candidate.name.startswith("test_")
    )


def imported_modules(source: str) -> set[str]:
    """Every module name a file imports directly, at any level of its body."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            # ``from modules import foo`` imports the module ``modules.foo``.
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _imports_any(imported: Iterable[str], changed_modules: set[str]) -> bool:
    for name in imported:
        if name in changed_modules:
            return True
        # Importing a submodule runs its package's __init__ too.
        if any(name.startswith(f"{module}.") for module in changed_modules):
            return True
    return False


@dataclass
class AffectedTests:
    files: list[str]
    #: Test files the change adds or edits itself.
    changed: list[str] = field(default_factory=list)
    #: Test files that import a changed module.
    importers: list[str] = field(default_factory=list)
    #: Files left out because of the cap.
    dropped: list[str] = field(default_factory=list)


def select_affected_test_files(
    changed_paths: Iterable[str],
    repo_root: str | Path,
    *,
    max_files: int | None = None,
) -> AffectedTests:
    """Test files the change adds or edits, plus test files importing a changed module.

    Changed test files come first, so the cap never drops what the change
    wrote itself. Only files that exist in *repo_root* are returned.
    """
    root = Path(repo_root)
    limit = max_files if max_files is not None else max_files_from_env()
    changed_tests: list[str] = []
    changed_modules: set[str] = set()
    for raw in changed_paths:
        path = raw.strip().strip('"')
        if not path or not (root / path).is_file():
            continue
        if _is_test_file(path):
            if path not in changed_tests:
                changed_tests.append(path)
        elif not path.startswith("tests/"):
            module = module_name_for_path(path)
            if module:
                changed_modules.add(module)

    importers: list[str] = []
    tests_dir = root / "tests"
    if changed_modules and tests_dir.is_dir():
        for test_file in sorted(tests_dir.rglob("test_*.py")):
            relative = test_file.relative_to(root).as_posix()
            if relative in changed_tests:
                continue
            try:
                source = test_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _imports_any(imported_modules(source), changed_modules):
                importers.append(relative)

    ordered = sorted(changed_tests) + importers
    return AffectedTests(
        files=ordered[:limit],
        changed=sorted(changed_tests),
        importers=importers,
        dropped=ordered[limit:],
    )


# --- Running them with drift -------------------------------------------------

# pytest's short summary (``-rfE``): "FAILED tests/x.py::C::t - message".
_SUMMARY_RE = re.compile(r"^(?:FAILED|ERROR) (?P<id>\S+?)(?: - .*)?$")


def parse_pytest_failures(output: str) -> list[str]:
    """Node ids from pytest's short test summary, in order, deduplicated."""
    ids: list[str] = []
    for line in (output or "").splitlines():
        match = _SUMMARY_RE.match(line.strip())
        if match and match.group("id") not in ids:
            ids.append(match.group("id"))
    return ids


@dataclass
class PytestRun:
    exit_code: int | None
    output: str
    failed_ids: list[str]
    timed_out: bool = False
    error: str | None = None


@dataclass
class TimingCheckResult:
    status: str
    timing_sensitive: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    drift_ms: float = DEFAULT_DRIFT_MS
    detail: str = ""
    output: str = ""

    @property
    def failed(self) -> bool:
        return self.status == STATUS_TIMING_SENSITIVE and bool(self.timing_sensitive)

    def error_message(self) -> str:
        return timing_sensitive_error(self.timing_sensitive)


def timing_sensitive_error(test_ids: Sequence[str]) -> str:
    shown = list(test_ids)[:_MAX_REPORTED_IDS]
    more = len(test_ids) - len(shown)
    listed = ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")
    return f"{TIMING_SENSITIVE_PREFIX}: {listed}. {TIMING_SENSITIVE_HINT}"


def pytest_command(python: str, targets: Sequence[str]) -> list[str]:
    return [
        python, "-m", "pytest", "-q", "-rfE", "--tb=short",
        # Never write .pytest_cache into the worktree, and run in one process
        # so a test sees the same setup as in the normal run.
        "-p", "no:cacheprovider", "-p", "no:xdist",
        *targets,
    ]


Runner = Callable[[list[str], str, dict[str, str], float], PytestRun]


def _run_pytest(
    command: list[str], cwd: str, env: dict[str, str], timeout: float,
    on_process: Callable[[subprocess.Popen | None], None] | None = None,
) -> PytestRun:
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, never a shell
            command, cwd=cwd, env=env, shell=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return PytestRun(None, "", [], error=f"could not start pytest: {exc}")
    if on_process is not None:
        on_process(process)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
        output, _ = process.communicate()
        return PytestRun(None, output or "", parse_pytest_failures(output or ""),
                         timed_out=True, error=f"timed out after {timeout:.0f}s")
    finally:
        if on_process is not None:
            on_process(None)
    return PytestRun(process.returncode, output or "", parse_pytest_failures(output or ""))


def _env_for(drift_ms: float | None, base_env: Mapping[str, str] | None) -> dict[str, str]:
    env = build_verification_test_env(base_env)
    env.pop(CLOCK_DRIFT_ENV_VAR, None)
    env["PYTHONUNBUFFERED"] = "1"
    # The check must not recurse into itself from inside the suite.
    env[TIMING_CHECK_ENV_VAR] = "0"
    if drift_ms is not None:
        env[CLOCK_DRIFT_ENV_VAR] = f"{drift_ms:g}"
    return env


def _clip(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return "...[truncated]...\n" + text[-_MAX_OUTPUT_CHARS:]


def run_timing_check(
    python: str,
    cwd: str,
    test_files: Sequence[str],
    *,
    drift_ms: float | None = None,
    timeout_seconds: float | None = None,
    base_env: Mapping[str, str] | None = None,
    on_process: Callable[[subprocess.Popen | None], None] | None = None,
    runner: Runner | None = None,
) -> TimingCheckResult:
    """Run *test_files* with clock drift; control-run whatever fails there."""
    if drift_ms is None:
        drift_ms = drift_from_env()
    if not test_files:
        return TimingCheckResult(STATUS_SKIPPED, drift_ms=drift_ms,
                                 detail="no affected test files")
    timeout = timeout_seconds if timeout_seconds is not None else timeout_from_env()
    started = time.monotonic()

    def run(targets: Sequence[str], drift: float | None) -> PytestRun:
        remaining = max(1.0, timeout - (time.monotonic() - started))
        command = pytest_command(python, targets)
        env = _env_for(drift, base_env)
        if runner is not None:
            return runner(command, cwd, env, remaining)
        return _run_pytest(command, cwd, env, remaining, on_process)

    files = list(test_files)
    drifted = run(files, drift_ms)
    if drifted.error and not drifted.timed_out:
        return TimingCheckResult(STATUS_INCONCLUSIVE, test_files=files, drift_ms=drift_ms,
                                 detail=drifted.error, output=_clip(drifted.output))
    if drifted.timed_out:
        return TimingCheckResult(STATUS_INCONCLUSIVE, test_files=files, drift_ms=drift_ms,
                                 detail=f"drift run {drifted.error}", output=_clip(drifted.output))
    # 5: pytest collected nothing - nothing to judge.
    if drifted.exit_code in (0, 5):
        return TimingCheckResult(STATUS_PASSED, test_files=files, drift_ms=drift_ms,
                                 output=_clip(drifted.output))
    if not drifted.failed_ids:
        return TimingCheckResult(
            STATUS_INCONCLUSIVE, test_files=files, drift_ms=drift_ms,
            detail=f"drift run exited {drifted.exit_code} without naming a failing test",
            output=_clip(drifted.output),
        )

    control = run(drifted.failed_ids, None)
    if control.error or control.timed_out or control.exit_code is None:
        return TimingCheckResult(
            STATUS_INCONCLUSIVE, test_files=files, drift_ms=drift_ms,
            detail=f"control run without drift: {control.error or 'no exit code'}",
            output=_clip(drifted.output + "\n--- control run without drift ---\n" + control.output),
        )
    if control.exit_code == 0:
        sensitive = list(drifted.failed_ids)
    elif control.failed_ids:
        sensitive = [test_id for test_id in drifted.failed_ids if test_id not in control.failed_ids]
    else:
        return TimingCheckResult(
            STATUS_INCONCLUSIVE, test_files=files, drift_ms=drift_ms,
            detail=f"control run exited {control.exit_code} without naming a failing test",
            output=_clip(drifted.output + "\n--- control run without drift ---\n" + control.output),
        )
    combined = _clip(drifted.output + "\n--- control run without drift ---\n" + control.output)
    if not sensitive:
        return TimingCheckResult(
            STATUS_PASSED, test_files=files, drift_ms=drift_ms,
            detail="every drift failure also fails without drift", output=combined,
        )
    return TimingCheckResult(STATUS_TIMING_SENSITIVE, timing_sensitive=sensitive,
                             test_files=files, drift_ms=drift_ms, output=combined)
