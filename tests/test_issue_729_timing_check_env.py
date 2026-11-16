from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.experiment_service.service_parts.verification import VerificationMixin
from modules.timing_check import TimingCheckResult, run_timing_check
from modules.verification_env import VERIFICATION_TEST_API_KEY, build_verification_test_env


def test_timing_check_uses_isolated_verification_environment(tmp_path: Path) -> None:
    test = tmp_path / "test_env.py"
    test.write_text(
        "import os\n"
        "def test_env():\n"
        f"    assert os.environ['AGENT_API_KEY'] == {VERIFICATION_TEST_API_KEY!r}\n"
        "    assert 'GITHUB_TOKEN' not in os.environ\n"
        "    assert 'AZURE_OPENAI_API_KEY' not in os.environ\n"
        "    assert os.environ['NEXUS_TEST_CLOCK_DRIFT_MS'] == '0'\n"
        "    assert os.environ['PYTHONUNBUFFERED'] == '1'\n",
        encoding="utf-8",
    )
    parent = {
        "AGENT_API_KEY": "parent-secret",
        "GITHUB_TOKEN": "github-secret",
        "AZURE_OPENAI_API_KEY": "azure-secret",
        "NEXUS_TEST_CLOCK_DRIFT_MS": "999",
    }
    with patch.dict(os.environ, parent):
        assert run_timing_check(tmp_path, ["test_env.py"], 0) == "ok"


def test_normal_verification_environment_drops_outer_timing_drift() -> None:
    env = build_verification_test_env({"NEXUS_TEST_CLOCK_DRIFT_MS": "50", "PATH": "/bin"})

    assert "NEXUS_TEST_CLOCK_DRIFT_MS" not in env
    assert env["PATH"] == "/bin"


def test_failed_test_has_a_bounded_failure_tail(tmp_path: Path) -> None:
    test = tmp_path / "test_failure.py"
    test.write_text(
        "def test_failure():\n"
        "    marker = 'diagnostic marker from timing check'\n"
        "    assert False, marker\n",
        encoding="utf-8",
    )
    result = run_timing_check(tmp_path, ["test_failure.py"], 0)
    test_id = "test_failure.py::test_failure"
    assert result == f"timing_sensitive: {test_id}"
    assert test_id in result.failure_tails
    assert len(result.failure_tails[test_id]) <= 40
    assert "diagnostic marker from timing check" in json.dumps(result.failure_tails[test_id])


def test_failure_tail_is_stored_on_flagged_and_failed_closed_events() -> None:
    test_id = "tests/test_x.py::test_failure"
    tails = {test_id: ["E assert diagnostic marker"]}
    result = TimingCheckResult(f"timing_sensitive: {test_id}", tails)
    service = MagicMock()
    service.get_experiment.return_value = SimpleNamespace(base_commit_sha=None)
    service.test_python_executable = None

    for control, event in [(([test_id], []), "timing_check_flagged"), (([], [test_id]), "timing_check_failed_closed")]:
        logger = MagicMock()
        with patch("modules.experiment_service.service_parts.verification.timing_check_enabled", return_value=True), \
                patch("modules.timing_check.affected_tests", return_value=["tests/test_x.py"]), \
                patch("modules.timing_check.run_timing_check", return_value=result), \
                patch("modules.timing_check.control_check", return_value=control):
            VerificationMixin._run_timing_check(service, "experiment", ".", "diff", logger)
        call = logger.error.call_args if event == "timing_check_flagged" else logger.warning.call_args
        assert call.args[1] == event
        assert call.kwargs["failure_tails"] == tails
