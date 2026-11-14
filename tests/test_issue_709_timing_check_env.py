"""Regression coverage for the timing check's environment and diagnostics."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

from modules.timing_check import TimingCheckResult, run_timing_check
from modules.experiment_service.service_parts.verification import VerificationMixin
from modules.verification_env import VERIFICATION_TEST_API_KEY


def test_timing_check_uses_sanitized_verification_environment(tmp_path: Path) -> None:
    (tmp_path / "test_env.py").write_text(
        "import os\n"
        "def test_env():\n"
        f"    assert os.environ['AGENT_API_KEY'] == {VERIFICATION_TEST_API_KEY!r}\n"
        "    assert 'GITHUB_TOKEN' not in os.environ\n",
        encoding="utf-8",
    )

    with patch.dict(
        os.environ,
        {"AGENT_API_KEY": "production-value", "GITHUB_TOKEN": "secret-value"},
    ):
        assert run_timing_check(tmp_path, ["test_env.py"], 0) == "ok"


def test_failed_test_has_a_bounded_output_tail(tmp_path: Path) -> None:
    (tmp_path / "test_failure.py").write_text(
        "def test_broken():\n"
        "    marker = 'diagnostic-from-failing-test'\n"
        "    assert marker == 'expected'\n",
        encoding="utf-8",
    )

    result = run_timing_check(tmp_path, ["test_failure.py"], 0)

    assert isinstance(result, TimingCheckResult)
    assert result == "timing_sensitive: test_failure.py::test_broken"
    tail = result.failure_tails["test_failure.py::test_broken"]
    assert len(tail) <= 40
    assert "diagnostic-from-failing-test" in "\n".join(tail)


def test_verification_timing_event_stores_failure_tails() -> None:
    node_id = "tests/test_failure.py::test_broken"
    drift_result = TimingCheckResult(
        f"timing_sensitive: {node_id}",
        {node_id: ["E assert diagnostic-from-failing-test"]},
    )
    service = SimpleNamespace(
        get_experiment=lambda _experiment_id: SimpleNamespace(base_commit_sha="base"),
        _files_changed_since=lambda *_args: {"tests/test_failure.py"},
        _track_process=lambda *_args: None,
        test_python_executable=None,
    )
    elog = MagicMock()

    with patch(
            "modules.experiment_service.service_parts.verification.timing_check_enabled",
            return_value=True,
        ), patch("modules.timing_check.affected_tests", return_value=["tests/test_failure.py"]), \
            patch("modules.timing_check.run_timing_check", return_value=drift_result), \
            patch("modules.timing_check.control_check", return_value=([node_id], [])):
        verdict = VerificationMixin._run_timing_check(
            service, "experiment", "/repo", "diff", elog,
        )

    assert verdict is not None and verdict[0] == "failed"
    event_kwargs = elog.error.call_args.kwargs
    assert event_kwargs["failure_tails"] == drift_result.failure_tails
