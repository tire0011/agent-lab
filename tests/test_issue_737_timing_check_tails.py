"""#737: per-test failure tails from pytest's junitxml, matched by exact node id."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from modules import timing_check as timing_check_module
from modules.experiment_evaluation import EvaluationResult
from modules.experiment_service import ExperimentService, STATUS_VERIFYING
from modules.timing_check import (
    TAIL_MAX_LINES,
    control_check,
    failure_tails,
    flagged_tests,
    run_timing_check,
)

PARAMETRIZED = (
    "import pytest\n"
    "@pytest.mark.parametrize('value', ['a', 'b', 'c'])\n"
    "def test_x(value):\n"
    "    assert value != 'b', 'only b fails'\n"
)
SAME_METHOD_NAME = (
    "class TestA:\n"
    "    def test_m(self):\n"
    "        assert False, 'failure of A'\n"
    "class TestB:\n"
    "    def test_m(self):\n"
    "        assert False, 'failure of B'\n"
)
LONG_FAILURE = (
    "def test_long():\n"
    "    print('FAILED tests/test_long.py::test_fake - printed by the test')\n"
    "    raise AssertionError('\\n'.join(f'line {n}' for n in range(200)))\n"
)


def _repo(tmp_path: Path, name: str, source: str) -> Path:
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / name).write_text(source, encoding="utf-8")
    return tmp_path


def test_only_the_failing_parameter_gets_a_tail(tmp_path: Path):
    repo = _repo(tmp_path, "test_p.py", PARAMETRIZED)
    tails: dict[str, str] = {}
    result = run_timing_check(repo, ["tests/test_p.py"], tails=tails)
    assert result == "timing_sensitive: tests/test_p.py::test_x[b]"
    assert flagged_tests(result) == ["tests/test_p.py::test_x[b]"]
    assert list(tails) == ["tests/test_p.py::test_x[b]"]
    assert "only b fails" in tails["tests/test_p.py::test_x[b]"]
    assert "tests/test_p.py::test_x[a]" not in tails
    assert "tests/test_p.py::test_x[c]" not in tails


def test_two_classes_with_the_same_method_get_their_own_tail(tmp_path: Path):
    repo = _repo(tmp_path, "test_c.py", SAME_METHOD_NAME)
    tails: dict[str, str] = {}
    result = run_timing_check(repo, ["tests/test_c.py"], tails=tails)
    assert flagged_tests(result) == ["tests/test_c.py::TestA::test_m", "tests/test_c.py::TestB::test_m"]
    assert set(tails) == {"tests/test_c.py::TestA::test_m", "tests/test_c.py::TestB::test_m"}
    assert "failure of A" in tails["tests/test_c.py::TestA::test_m"]
    assert "failure of B" not in tails["tests/test_c.py::TestA::test_m"]
    assert "failure of B" in tails["tests/test_c.py::TestB::test_m"]
    assert "failure of A" not in tails["tests/test_c.py::TestB::test_m"]


def test_a_tail_is_capped_at_40_lines(tmp_path: Path):
    repo = _repo(tmp_path, "test_long.py", LONG_FAILURE)
    tails: dict[str, str] = {}
    result = run_timing_check(repo, ["tests/test_long.py"], tails=tails)
    # A FAILED line the test prints itself is not a flagged test.
    assert result == "timing_sensitive: tests/test_long.py::test_long"
    tail = tails["tests/test_long.py::test_long"]
    assert TAIL_MAX_LINES == 40
    assert len(tail.splitlines()) == 40
    assert "line 199" in tail


def test_control_check_passes_tails_on(tmp_path: Path):
    repo = _repo(tmp_path, "test_p.py", PARAMETRIZED)
    tails: dict[str, str] = {}
    flagged = ["tests/test_p.py::test_x[a]", "tests/test_p.py::test_x[b]"]
    assert control_check(repo, flagged, tails=tails) == (
        ["tests/test_p.py::test_x[a]"], ["tests/test_p.py::test_x[b]"],
    )
    assert list(tails) == ["tests/test_p.py::test_x[b]"]


def test_failure_tails_matches_classname_and_name_exactly(tmp_path: Path):
    report = tmp_path / "junit.xml"
    long_text = "\n".join(f"row {n}" for n in range(100))
    report.write_text(
        "<testsuites><testsuite>"
        '<testcase classname="tests.test_p" name="test_x[a]" />'
        f'<testcase classname="tests.test_p" name="test_x[b]"><failure message="m">{long_text}</failure></testcase>'
        '<testcase classname="tests.test_p.TestA" name="test_m"><error message="setup broke" /></testcase>'
        '<testcase classname="tests.test_p.TestB" name="test_m"><skipped message="skip" /></testcase>'
        '<testcase classname="nowhere.mod" name="test_lost"><failure>gone</failure></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_p.py").write_text("", encoding="utf-8")
    # No known ids: the node ids come from the file found under the repo.
    tails = failure_tails(report, repo=tmp_path)
    assert set(tails) == {"tests/test_p.py::test_x[b]", "tests/test_p.py::TestA::test_m"}
    assert tails["tests/test_p.py::TestA::test_m"] == "setup broke"
    assert tails["tests/test_p.py::test_x[b]"].splitlines() == [f"row {n}" for n in range(60, 100)]
    # Known ids win over the file lookup.
    assert set(failure_tails(report, ["tests/test_p.py::test_x[b]"])) == {"tests/test_p.py::test_x[b]"}


def test_unreadable_report_keeps_the_verdict(tmp_path: Path):
    repo = _repo(tmp_path, "test_p.py", PARAMETRIZED)
    tails: dict[str, str] = {}
    with patch("modules.timing_check.failure_tails", side_effect=OSError("gone")):
        result = run_timing_check(repo, ["tests/test_p.py"], tails=tails)
    assert result == "timing_sensitive: tests/test_p.py::test_x[b]"
    assert tails == {}


# --- verification events -------------------------------------------------------------

@pytest.fixture
def service(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    # A unittest case: verification discovers with unittest, and Python 3.12+
    # exits 5 when discovery finds no tests (a bare pytest function).
    (repo / "tests" / "test_new.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
        encoding="utf-8",
    )

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True).stdout

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    worker = MagicMock()
    worker.run.return_value = SimpleNamespace(usage=None, timed_out=False, error=None)
    svc = ExperimentService(
        db_path=str(tmp_path / "db.sqlite"), repo_root=str(repo),
        worktrees_root=str(tmp_path / "worktrees"), test_python_executable=sys.executable,
        experiment_log_dir=str(tmp_path / "logs"), max_repair_iterations=0,
        worker_factory=lambda _: worker, plan_worker_factory=lambda _: None,
    )
    quality = patch(
        "modules.experiment_service.evaluate_implementation",
        return_value=EvaluationResult(outcome="pass", reason="ok"),
    )
    quality.start()
    try:
        yield svc, repo, git("rev-parse", "HEAD").strip()
    finally:
        quality.stop()
        svc.close()


def _events(svc, experiment_id: str, event: str) -> list[dict]:
    path = svc.get_experiment_logger(experiment_id).path
    entries = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]
    return [entry for entry in entries if entry.get("event") == event]


def _verify(svc, repo: Path, base: str):
    experiment = svc.create_experiment("Add a test")
    svc._force_status(experiment.id, STATUS_VERIFYING, base_commit_sha=base)
    with patch.dict("os.environ", {"NEXUS_TIMING_CHECK": "1"}):
        return experiment.id, svc._verify(experiment.id, str(repo))


def _fake_run(drift_tails: dict[str, str], control_tails: dict[str, str], control_result: str):
    def run(repo, tests, drift_ms=50, *, on_process=None, python=None, tails=None, env=None):
        if tails is not None:
            tails.update(drift_tails if drift_ms else control_tails)
        if drift_ms:
            return "timing_sensitive: " + ", ".join(drift_tails)
        return control_result
    return run


def test_flagged_event_carries_tails_of_drift_only_tests(service):
    svc, repo, base = service
    a, b = "tests/test_new.py::test_x[a]", "tests/test_new.py::test_x[b]"
    run = _fake_run({a: "drift a", b: "drift b"}, {b: "control b"}, f"timing_sensitive: {b}")
    with patch("modules.timing_check.run_timing_check", side_effect=run):
        experiment_id, result = _verify(svc, repo, base)
    assert result.status == "failed"
    assert result.error.startswith(f"timing_sensitive: {a}; hint:")
    (event,) = _events(svc, experiment_id, "timing_check_flagged")
    assert event["tails"] == {a: "drift a"}
    assert event["fails_without_drift"] == str([b])  # lists stay stringified


def test_failed_closed_event_carries_control_tails(service):
    svc, repo, base = service
    b = "tests/test_new.py::test_x[b]"
    run = _fake_run({b: "drift b"}, {b: "control b"}, f"timing_sensitive: {b}")
    with patch("modules.timing_check.run_timing_check", side_effect=run):
        experiment_id, result = _verify(svc, repo, base)
    assert result.status == "needs_review"
    assert "fail on this host without drift too" in result.error
    (event,) = _events(svc, experiment_id, "timing_check_failed_closed")
    assert event["tails"] == {b: "control b"}


def test_failed_closed_without_control_carries_drift_tails(service):
    svc, repo, base = service
    b = "tests/test_new.py::test_x[b]"
    run = _fake_run({b: "drift b"}, {}, "error")
    with patch("modules.timing_check.run_timing_check", side_effect=run):
        experiment_id, result = _verify(svc, repo, base)
    assert result.error == "timing check failed closed: error: control run without drift gave no verdict"
    (event,) = _events(svc, experiment_id, "timing_check_failed_closed")
    assert event["tails"] == {b: "drift b"}


def test_logger_keeps_a_flat_text_map_structured_but_sanitized(tmp_path: Path):
    from modules.central_logging import MAX_EXTRA_MAP_ENTRIES, ExperimentLogger

    elog = ExperimentLogger("exp-tails", log_dir=str(tmp_path))
    tails = {f"t.py::test_{n}": f"tail {n}" for n in range(MAX_EXTRA_MAP_ENTRIES + 5)}
    tails["t.py::test_0"] = "api_key=sk-super-secret-value-123456"
    elog.warning(
        "verifying", "timing_check_failed_closed", "x", tails=tails, other=[1, 2],
        classifications={"t.T.test": "pre_existing"},
    )
    entry = json.loads(Path(elog.path).read_text(encoding="utf-8").splitlines()[0])
    assert isinstance(entry["tails"], dict)
    assert len(entry["tails"]) == MAX_EXTRA_MAP_ENTRIES
    assert entry["tails"]["t.py::test_1"] == "tail 1"
    assert "sk-super-secret-value-123456" not in json.dumps(entry)
    assert entry["truncated"] is True
    assert entry["other"] == "[1, 2]"  # other structures are still stringified
    # Only the allowed field is structured; other flat maps stay strings.
    assert entry["classifications"] == str({"t.T.test": "pre_existing"})


def test_parameter_ids_with_separators_match_exactly(tmp_path: Path):
    repo = _repo(tmp_path, "test_s.py", (
        "import pytest\n"
        "@pytest.mark.parametrize('v', ['a::b', 'c/d.e', 'f'], ids=['a::b', 'c/d.e', 'f'])\n"
        "def test_s(v):\n"
        "    assert v == 'f', v\n"
    ))
    tails: dict[str, str] = {}
    run_timing_check(repo, ["tests/test_s.py"], tails=tails)
    assert set(tails) == {"tests/test_s.py::test_s[a::b]", "tests/test_s.py::test_s[c/d.e]"}
