from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.gate_preflight import PreflightResult, preflight
from modules.nexus_initiative import Candidate, KIND_ISSUE, NexusInitiativeService
from modules.verification_env import VERIFICATION_TEST_API_KEY


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    for name, source in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "main"], cwd=repo, check=True, capture_output=True)
    return repo


def test_failing_named_test_blocks_and_captures_tail(tmp_path: Path):
    repo = _repo(tmp_path, {"tests/test_named.py": "def test_bad():\n    assert False\n"})
    result = preflight("Please fix `tests/test_named.py`.", repo)
    assert result.status == "blocked"
    assert result.failing_ids == ["tests/test_named.py::test_bad"]
    assert "assert False" in (result.tails or {})[result.failing_ids[0]]


def test_green_main_passes_and_uses_verification_environment(tmp_path: Path):
    repo = _repo(
        tmp_path,
        {
            "tests/test_env.py": (
                "import os\n"
                "def test_env():\n"
                "    assert 'GITHUB_TOKEN' not in os.environ\n"
                f"    assert os.environ['AGENT_API_KEY'] == {VERIFICATION_TEST_API_KEY!r}\n"
            )
        },
    )
    with patch.dict(os.environ, {"GITHUB_TOKEN": "secret", "AGENT_API_KEY": "production"}):
        result = preflight("tests/test_env.py", repo)
    assert result.status == "passed"


def test_named_module_selects_importing_test(tmp_path: Path):
    repo = _repo(
        tmp_path,
        {
            "modules/worker.py": "VALUE = 1\n",
            "tests/test_worker.py": "from modules.worker import VALUE\ndef test_value(): assert VALUE == 2\n",
        },
    )
    result = preflight("Change modules/worker.py", repo)
    assert result.status == "blocked"
    assert result.failing_ids == ["tests/test_worker.py::test_value"]


def test_budget_and_selection_errors_fail_open(tmp_path: Path):
    repo = _repo(tmp_path, {"tests/test_x.py": "def test_x(): assert False\n"})
    assert preflight("tests/test_x.py", repo, budget_s=0).status == "skipped"
    with patch("modules.gate_preflight._selection", side_effect=OSError("broken")):
        assert preflight("tests/test_x.py", repo).status == "skipped"


def _initiative(tmp_path: Path):
    launcher = SimpleNamespace(handle_issue=MagicMock())
    experiment_service = SimpleNamespace(repo_root=str(tmp_path))
    service = NexusInitiativeService(
        db_path=str(tmp_path / "initiative.db"),
        experiment_service=experiment_service,
        controller=launcher,
        daily_budget=5,
        reflection_sweep=lambda **kwargs: None,
    )
    candidate = Candidate(KIND_ISSUE, "745", "Gate", "next", {"body": "tests/test_gate.py"})
    patches = (
        patch.object(service, "_paused", return_value=False),
        patch.object(service, "_sweep_stalled_experiments"),
        patch.object(service, "enforce_issue_approvals"),
        patch.object(service, "busy_reason", return_value=None),
        patch.object(service, "_stale_service_revisions", return_value=None),
        patch.object(service, "_spent_today", return_value=0),
        patch.object(service, "candidates", return_value=[candidate]),
        patch.object(service, "_execution_allowed", return_value=SimpleNamespace(allowed=True)),
    )
    return service, launcher, patches


def test_blocked_preflight_logs_once_and_does_not_launch(tmp_path: Path):
    service, launcher, patches = _initiative(tmp_path)
    result = PreflightResult("blocked", ["tests/test_gate.py::test_bad"], {"tests/test_gate.py::test_bad": "tail"}, 1)
    try:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
                patch("modules.gate_preflight.preflight", return_value=result):
            decision = service.tick()
        assert decision.decision == "preflight_blocked"
        launcher.handle_issue.assert_not_called()
        assert [row["decision"] for row in service.history()] == ["preflight_blocked"]
    finally:
        service.close()


def test_passed_or_skipped_launches_and_flag_off_does_not_call_preflight(tmp_path: Path):
    for verdict, flag in (("passed", "1"), ("skipped", "1"), ("blocked", "0")):
        case = tmp_path / f"case-{verdict}-{flag}"
        case.mkdir()
        service, launcher, patches = _initiative(case)
        try:
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], \
                    patch.dict(os.environ, {"NEXUS_GATE_PREFLIGHT": flag}), \
                    patch("modules.gate_preflight.preflight", return_value=PreflightResult(verdict)) as check:
                decision = service.tick()
            assert decision.decision == "dispatched"
            launcher.handle_issue.assert_called_once()
            assert check.call_count == (0 if flag == "0" else 1)
        finally:
            service.close()
