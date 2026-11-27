"""Find and rerun tests which may depend on changed timing code."""

from __future__ import annotations

import ast
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from modules.verification_runner import HungTest, WatchdogConfig, run_verification_test_monitored
from modules.verification_env import CLOCK_DRIFT_ENV, build_verification_test_env


_DIFF_PATH = re.compile(r"^(?:diff --git a/(\S+) b/(\S+)|\+\+\+ b/(\S+))$", re.MULTILINE)
_FAILED_TEST = re.compile(r"^FAILED\s+(.+?)(?:\s+-\s+.*)?$", re.MULTILINE)
_SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$", re.MULTILINE)
# At most this many lines of failure or error text are kept per test (#737).
TAIL_MAX_LINES = 40


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
    tails: dict[str, str] | None = None,
    watchdog: WatchdogConfig | None = None,
    run_id: str = "timing-check",
) -> tuple[list[str], list[str]] | None:
    """Rerun flagged tests without drift: ``(drift_only, fails_without_drift)``.

    A test that also fails without drift is not shown to be timing-sensitive -
    it fails on this host anyway (a missing test dependency, host state), for
    instance when the normal run that passed was GitHub CI. Returns None when
    the control run gives no verdict. *tails* receives the control run's
    failure tails, as in ``run_timing_check``.
    """
    tests = list(flagged)
    # Drift 0 keeps the fixture installed but adds nothing, so the clocks are real.
    kwargs = {"tails": tails} if tails is not None else {}
    if watchdog is not None:
        kwargs.update(watchdog=watchdog, run_id=run_id)
    control = run_timing_check(repo, tests, 0, on_process=on_process, python=python, **kwargs)
    if control == "ok":
        return tests, []
    also_failing = set(flagged_tests(control))
    if not also_failing:
        return None
    return (
        [test for test in tests if test not in also_failing],
        [test for test in tests if test in also_failing],
    )


def _junit_key(node_id: str) -> tuple[str, str]:
    """``(classname, name)`` pytest's junitxml writes for *node_id*.

    The same mangling as ``_pytest.junitxml.mangle_test_address``: the path
    loses ``.py`` and its slashes become dots, the classes follow, and the last
    part (parameters included) is the name.
    """
    # Parameter ids may contain "::" or "/" themselves, so split only before "[".
    bracket = node_id.find("[")
    head, params = (node_id[:bracket], node_id[bracket:]) if bracket >= 0 else (node_id, "")
    path, *names = head.split("::")
    names = [name for name in names if name != "()"]
    if names:
        names[-1] += params
    parts = path.replace("\\", "/").split("/")
    parts[-1] = parts[-1][:-3] if parts[-1].endswith(".py") else parts[-1]
    names = [".".join(parts), *names]
    return ".".join(names[:-1]), names[-1]


def _resolve_node_id(repo: Path, classname: str, name: str) -> str | None:
    """Node id of a testcase that is not among the known ids: find its file."""
    parts = classname.split(".") if classname else []
    for split in range(len(parts), 0, -1):
        candidate = "/".join(parts[:split]) + ".py"
        if (repo / candidate).is_file():
            return "::".join([candidate, *parts[split:], name])
    return None


def _cap(text: str) -> str:
    """The last TAIL_MAX_LINES lines of *text* (the assertion is at the end)."""
    lines = text.strip("\n").splitlines()
    return "\n".join(lines[-TAIL_MAX_LINES:])


def failure_tails(
    junit_xml: str | os.PathLike[str],
    known_ids: Iterable[str] = (),
    repo: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """``{node_id: text}`` for every failed or erroring ``<testcase>`` (#737).

    A testcase maps to its full node id, parameters included, by exact
    ``(classname, name)`` - never by function name alone - so ``test_x[b]``
    gets only its own text and two classes with the same method name each get
    theirs. *known_ids* (the ids pytest printed) are matched first; any other
    testcase is resolved by finding its file under *repo*, and skipped when
    there is none. Each text is capped at TAIL_MAX_LINES lines.
    """
    by_key: dict[tuple[str, str], str] = {}
    for node_id in known_ids:
        by_key.setdefault(_junit_key(node_id), node_id)
    root = ElementTree.parse(junit_xml).getroot()
    tails: dict[str, str] = {}
    for case in root.iter("testcase"):
        texts = []
        for child in case:
            if child.tag not in ("failure", "error"):
                continue
            text = (child.text or "").strip("\n") or child.get("message", "")
            if text:
                texts.append(text)
        if not texts:
            continue
        classname, name = case.get("classname", ""), case.get("name", "")
        node_id = by_key.get((classname, name))
        if node_id is None and repo is not None:
            node_id = _resolve_node_id(Path(repo), classname, name)
        if node_id is None:
            continue
        joined = "\n".join(texts) if node_id not in tails else tails[node_id] + "\n" + "\n".join(texts)
        tails[node_id] = _cap(joined)
    return tails


def _failed_ids(output: str) -> list[str]:
    """``FAILED`` ids from the short test summary (tracebacks may print the word)."""
    headers = list(_SUMMARY_HEADER.finditer(output))
    summary = output[headers[-1].end():] if headers else output
    return list(dict.fromkeys(_FAILED_TEST.findall(summary)))


def drift_env(drift_ms: int | float = 50) -> dict[str, str]:
    """The environment of a drift run.

    The same isolated environment as the normal verification run (#736): no
    production API key or provider credentials, AGENT_API_KEY = test key, and
    no inherited drift - *drift_ms* is the only drift the child sees. The
    drift fixture is deterministic (no random seed), so passing one dict to
    two runs gives both the identical setup.
    """
    env = build_verification_test_env()
    env[CLOCK_DRIFT_ENV] = str(drift_ms)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _collected_ids(
    repo: Path, files: Iterable[str], env: dict[str, str], python: str | None,
) -> set[str] | None:
    """Node ids pytest collects from *files* in *repo*; None without an answer."""
    file_list = sorted(set(files))
    if not file_list:
        return set()
    try:
        proc = subprocess.run(
            [python or sys.executable, "-m", "pytest", "--collect-only", "-q",
             "-p", "no:cacheprovider", *file_list],
            cwd=repo, env=env, capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # 0 = collected, 5 = nothing collected; anything else is a collection error.
    if proc.returncode not in (0, 5):
        return None
    return {line.strip() for line in proc.stdout.splitlines() if "::" in line}


def base_drift_check(
    base_commit: str,
    repo: str | os.PathLike[str],
    test_ids: Iterable[str],
    env: dict[str, str],
    *,
    python: str | None = None,
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
    tails: dict[str, str] | None = None,
    watchdog: WatchdogConfig | None = None,
    run_id: str = "timing-check-base",
) -> tuple[list[str], list[str]] | None:
    """Rerun flagged tests on *base_commit* under the same drift (#725).

    Returns ``(regressed, base_drift)``: ``regressed`` passes on the base under
    drift (or does not exist there - a new test), ``base_drift`` fails on the
    base under drift too, so the change did not make it timing-sensitive.
    *env* must be the environment of the branch's drift run. The base is
    checked out in a temporary detached worktree of *repo*, removed afterwards.
    None when the base run gives no verdict - the caller then keeps failing.
    """
    tests = list(dict.fromkeys(test_ids))
    if not tests:
        return [], []
    parent = Path(tempfile.mkdtemp(prefix="nexus-timing-base-"))
    base_path = parent / "worktree"
    added = False
    try:
        add = subprocess.run(
            ["git", "worktree", "add", "--detach", str(base_path), base_commit],
            cwd=Path(repo), capture_output=True, text=True, timeout=120,
        )
        if add.returncode != 0:
            return None
        added = True
        present_files = {
            test.split("::", 1)[0] for test in tests
            if (base_path / test.split("::", 1)[0]).is_file()
        }
        collected = _collected_ids(base_path, present_files, env, python)
        if collected is None:
            return None
        on_base = [test for test in tests if test in collected]
        missing = [test for test in tests if test not in collected]
        kwargs: dict[str, object] = {"tails": tails} if tails is not None else {}
        if watchdog is not None:
            kwargs.update(watchdog=watchdog, run_id=run_id)
        result = _run_timing_check_env(
            base_path, on_base, env, on_process=on_process, python=python, **kwargs,
        )
        if result == "ok":
            failing_on_base: set[str] = set()
        else:
            failing_on_base = set(flagged_tests(result))
            if not failing_on_base:
                return None
        regressed = [
            test for test in tests if test in missing or test not in failing_on_base
        ]
        base_drift = [test for test in on_base if test in failing_on_base]
        return regressed, base_drift
    except HungTest:
        raise
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if added:
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(base_path)],
                    cwd=Path(repo), capture_output=True, text=True, timeout=120,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        shutil.rmtree(parent, ignore_errors=True)
        try:
            subprocess.run(
                ["git", "worktree", "prune"], cwd=Path(repo),
                capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            pass


def run_timing_check(
    repo: str | os.PathLike[str],
    tests: Iterable[str],
    drift_ms: int | float = 50,
    *,
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
    python: str | None = None,
    tails: dict[str, str] | None = None,
    watchdog: WatchdogConfig | None = None,
    run_id: str = "timing-check",
    env: dict[str, str] | None = None,
    require_active_drift: bool = False,
) -> str:
    """Run every selected test under clock drift and summarize the outcome.

    *python* is the interpreter for the pytest child (default: this one), so
    verification can use the same interpreter as its normal test run. When
    *tails* is a dict, it receives ``{node_id: failure text}`` (at most
    TAIL_MAX_LINES lines each) from pytest's junitxml report; the result
    string stays the same either way. *env* replaces ``drift_env(drift_ms)``
    (then *drift_ms* is ignored), so a rerun elsewhere - the base commit
    (#725) - sees exactly the same drift setup.
    *require_active_drift* checks the injected clocks around each test call.
    """

    env = drift_env(drift_ms) if env is None else env
    return _run_timing_check_env(
        repo, tests, env, on_process=on_process, python=python, tails=tails,
        watchdog=watchdog, run_id=run_id, require_active_drift=require_active_drift,
    )


_PYTEST_MAIN = r"""
import os
import sys
import time

real_clocks = {name: getattr(time, name) for name in ('monotonic', 'perf_counter', 'time')}

import pytest

plugins = []
"""

# Pytest captures stderr during tests. Duplicate it before capture and emit
# start/end events in the format understood by the shared progress watchdog.
_PYTEST_PROGRESS = r"""
progress = os.dup(2)

class Progress:
    def pytest_runtest_logstart(self, nodeid):
        os.write(progress, (nodeid + " ... ").encode())
        self.result = b"ok"

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.result = b"FAIL"
        elif report.skipped and self.result == b"ok":
            self.result = b"skipped"

    def pytest_runtest_logfinish(self, nodeid):
        os.write(progress, self.result + b"\n")

plugins.append(Progress())
"""

# Run inside the flagged test's fixture lifetime. A separate test would get
# freshly patched clocks and could miss a restoration in the candidate test.
_PYTEST_DRIFT_CANARY = r"""
class DriftCanary:
    def __init__(self, drift_seconds):
        if not 0 < drift_seconds < float('inf'):
            raise ValueError('the clock drift canary requires positive finite drift')
        self.minimum_offset = drift_seconds / 2

    def check_clocks(self):
        for name, original in real_clocks.items():
            clock = getattr(time, name)
            if clock is original or not clock_offset_ok(clock, original, self.minimum_offset):
                pytest.fail(f'clock drift canary: time.{name} has no injected drift', pytrace=False)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        self.check_clocks()
        try:
            return (yield)
        finally:
            self.check_clocks()

def clock_offset_ok(clock, original, minimum_offset):
    before = original()
    return clock() - before >= minimum_offset

plugins.append(DriftCanary(float(sys.argv.pop(1))))
"""


def _run_timing_check_env(
    repo: str | os.PathLike[str],
    tests: Iterable[str],
    env: dict[str, str],
    *,
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
    python: str | None = None,
    tails: dict[str, str] | None = None,
    watchdog: WatchdogConfig | None = None,
    run_id: str = "timing-check",
    require_active_drift: bool = False,
) -> str:
    """Implementation shared with the old-vs-new environment probe (#738)."""

    test_list = list(tests)
    if not test_list:
        return "ok"
    proc: subprocess.Popen[str] | None = None
    result = "error"
    report_dir = tempfile.TemporaryDirectory(prefix="nexus-timing-check-")
    report = Path(report_dir.name) / "junit.xml"
    try:
        arguments = ["-q", "-p", "no:cacheprovider", "--tb=short", f"--junitxml={report}", *test_list]
        executable = python or sys.executable
        if watchdog is not None or require_active_drift:
            script = _PYTEST_MAIN
            if watchdog is not None:
                script += _PYTEST_PROGRESS
            if require_active_drift:
                script += _PYTEST_DRIFT_CANARY
                arguments.insert(0, str(float(env[CLOCK_DRIFT_ENV]) / 1000))
            script += "\nsys.exit(pytest.main(sys.argv[1:], plugins=plugins))\n"
            command = [executable, "-c", script, *arguments]
        else:
            command = [executable, "-m", "pytest", *arguments]
        if watchdog is None:
            proc = subprocess.Popen(
                command,
                cwd=Path(repo), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, start_new_session=True,
            )
            if on_process is not None:
                on_process(proc)
            output, _ = proc.communicate()
            returncode = proc.returncode
        else:
            monitored = run_verification_test_monitored(
                command,
                cwd=str(repo), config=watchdog, run_id=run_id,
                on_process=on_process, tests_only_progress=True, test_env=env,
            )
            if monitored.stalled:
                raise HungTest(monitored.aborted_test_id or "no active pytest test reported")
            output, returncode = monitored.output, monitored.exit_code
        if returncode == 0:
            result = "ok"
        elif returncode == 1:
            failed = _failed_ids(output or "")
            result = f"{TIMING_SENSITIVE_PREFIX}{', '.join(failed)}" if failed else "error"
        if tails is not None and returncode != 0 and report.is_file():
            try:
                tails.update(failure_tails(report, _failed_ids(output or ""), repo))
            except (ElementTree.ParseError, OSError):
                pass  # tails are diagnostics; the verdict does not depend on them
    except HungTest:
        raise
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
        report_dir.cleanup()
    return result


def _probe_status(result: str, node_id: str) -> str:
    if result == "ok":
        return "PASS"
    failed = flagged_tests(result)
    if failed:
        return "FAIL" if node_id in failed else "PASS"
    return "ERROR"


def build_timing_check_env_probe_report(
    ids: Iterable[str],
    old_result: str,
    new_result: str,
    old_tails: dict[str, str] | None = None,
    new_tails: dict[str, str] | None = None,
) -> str:
    """Build the Markdown evidence table and #737 failure tails for #738."""

    node_ids = list(ids)
    lines = [
        "| test id | old env | verification env |",
        "| --- | --- | --- |",
    ]
    for node_id in node_ids:
        label = node_id.replace("|", r"\|")
        lines.append(
            f"| `{label}` | {_probe_status(old_result, node_id)} | "
            f"{_probe_status(new_result, node_id)} |"
        )
    for heading, tails in (("Old env failure tails", old_tails or {}),
                           ("Verification env failure tails", new_tails or {})):
        lines.extend(["", f"### {heading}"])
        if not tails:
            lines.extend(["", "_None._"])
            continue
        for node_id in node_ids:
            if node_id not in tails:
                continue
            # A tail can itself contain a fence; four backticks keep the report valid.
            lines.extend(["", f"`{node_id}`", "", "````text", tails[node_id], "````"])
    return "\n".join(lines)


def timing_check_env_probe(ids: Iterable[str], drift_ms: int | float = 0) -> str:
    """Run *ids* once with the inherited env and once with verification env.

    This is a one-off diagnostic for #738. It runs from the current checkout
    and returns Markdown suitable for posting on #714 and #729.
    """

    node_ids = list(ids)
    old_env = os.environ.copy()
    new_env = build_verification_test_env()
    for env in (old_env, new_env):
        env[CLOCK_DRIFT_ENV] = str(drift_ms)
        env["PYTHONUNBUFFERED"] = "1"
    old_tails: dict[str, str] = {}
    new_tails: dict[str, str] = {}
    old_result = _run_timing_check_env(Path.cwd(), node_ids, old_env, tails=old_tails)
    new_result = _run_timing_check_env(Path.cwd(), node_ids, new_env, tails=new_tails)
    return build_timing_check_env_probe_report(
        node_ids, old_result, new_result, old_tails, new_tails,
    )
