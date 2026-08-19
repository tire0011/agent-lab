"""Tests for modules/multi_provider_orchestrator.py."""

from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.llama_cpp_client import LlamaCppClientError
from modules.llama_router import RouterDecision
from modules.multi_provider_orchestrator import (
    ContextResult,
    DeveloperResult,
    MultiProviderOrchestrator,
    OrchestratorResult,
    ReviewerResult,
    VerificationResult,
    _build_final_response,
    _is_worktree_clean,
    _list_tracked_files,
    _log_provider_usage,
    _parse_developer_response,
    _parse_pytest_summary,
    _parse_reviewer_response,
    _run_git_diff,
    _run_git_log,
    _run_git_status,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def prompts_dir(tmp_path: Path) -> Path:
    """Create minimal prompt templates for all orchestrator roles."""
    templates = {
        "router_prompt.md": textwrap.dedent("""\
            ---
            version: "2.0"
            role: router
            ---
            Task: {{task}}
        """),
        "context_prompt.md": textwrap.dedent("""\
            ---
            version: "1.1"
            role: context_gatherer
            ---
            Task: {{task}}
            Diff: {{git_diff}}
            Files: {{tracked_files}}
            Log: {{git_log}}
            Status: {{git_status}}
        """),
        "planner_prompt.md": textwrap.dedent("""\
            ---
            version: "1.1"
            role: planner
            ---
            Task: {{task}}
            Context: {{context}}
        """),
        "developer_prompt.md": textwrap.dedent("""\
            ---
            version: "1.0"
            role: developer
            ---
            Sub-task: {{sub_task}}
            Context: {{context}}
        """),
        "reviewer_prompt.md": textwrap.dedent("""\
            ---
            version: "1.0"
            role: reviewer
            ---
            Tests passed: {{tests_passed}}
            Tests run: {{tests_run}}
            Tests failed: {{tests_failed}}
            Test output: {{test_output}}
            Diff: {{git_diff}}
            Files: {{files_changed}}
        """),
    }
    for name, content in templates.items():
        (tmp_path / name).write_text(content)
    return tmp_path


def _make_llama(responses: list[str]) -> MagicMock:
    """Build a mock LlamaCppClient that returns *responses* in order."""
    mock = MagicMock()
    mock.complete = AsyncMock(side_effect=responses)
    return mock


def _context_json(**kwargs) -> str:
    defaults = {
        "relevant_files": ["README.md"],
        "git_diff_summary": "no recent changes",
        "state_summary": "stable",
        "context_package": "context text",
    }
    defaults.update(kwargs)
    return json.dumps(defaults)


def _plan_json(**kwargs) -> str:
    defaults = {
        "goal": "Fix thing",
        "sub_tasks": [{"description": "do it", "provider_hint": "local"}],
    }
    defaults.update(kwargs)
    return json.dumps(defaults)


def _developer_json(**kwargs) -> str:
    defaults = {
        "files_changed": ["module.py"],
        "summary": "fixed it",
        "notes": "",
    }
    defaults.update(kwargs)
    return json.dumps(defaults)


def _reviewer_json(**kwargs) -> str:
    defaults = {
        "verdict": "pass",
        "issues": [],
        "suggested_tests": [],
        "summary": "all good",
    }
    defaults.update(kwargs)
    return json.dumps(defaults)


# ── Unit tests: _parse_pytest_summary ─────────────────────────────────────────

def test_parse_pytest_summary_pass():
    output = "10 passed in 1.23s"
    total, failed = _parse_pytest_summary(output)
    assert total == 10
    assert failed == 0


def test_parse_pytest_summary_with_failures():
    output = "3 failed, 7 passed in 2.5s"
    total, failed = _parse_pytest_summary(output)
    assert total == 10
    assert failed == 3


def test_parse_pytest_summary_empty():
    total, failed = _parse_pytest_summary("")
    assert total == 0
    assert failed == 0


# ── Unit tests: _parse_developer_response ─────────────────────────────────────

def test_parse_developer_response_json():
    raw = json.dumps({"files_changed": ["a.py"], "summary": "done", "notes": ""})
    result = _parse_developer_response(raw, provider_hint="local", provider_used="local")
    assert result.files_changed == ["a.py"]
    assert result.summary == "done"
    assert result.provider_hint == "local"
    assert result.provider_used == "local"


def test_parse_developer_response_fallback_on_invalid_json():
    raw = "plain text response with no json"
    result = _parse_developer_response(raw, provider_hint="azure_openai", provider_used="azure_openai")
    assert result.provider_used == "azure_openai"
    assert "plain text" in result.summary


# ── Unit tests: _parse_reviewer_response ──────────────────────────────────────

def test_parse_reviewer_response_pass():
    verification = VerificationResult(
        git_diff="", test_output="10 passed", tests_passed=True, tests_run=10, tests_failed=0
    )
    raw = json.dumps({"verdict": "pass", "issues": [], "suggested_tests": [], "summary": "ok"})
    result = _parse_reviewer_response(raw, verification)
    assert result.verdict == "pass"


def test_parse_reviewer_response_fail_overrides_llm():
    """If tests actually failed, verdict must be fail even if LLM says pass."""
    verification = VerificationResult(
        git_diff="", test_output="1 failed", tests_passed=False, tests_run=5, tests_failed=1
    )
    raw = json.dumps({"verdict": "pass", "issues": [], "suggested_tests": [], "summary": "looks ok"})
    result = _parse_reviewer_response(raw, verification)
    assert result.verdict == "fail"


def test_parse_reviewer_response_pass_with_notes():
    verification = VerificationResult(
        git_diff="", test_output="10 passed", tests_passed=True, tests_run=10, tests_failed=0
    )
    raw = json.dumps({"verdict": "pass_with_notes", "issues": [{"severity": "minor", "file": "x.py", "description": "typo"}], "suggested_tests": [], "summary": "ok with notes"})
    result = _parse_reviewer_response(raw, verification)
    assert result.verdict == "pass_with_notes"


def test_parse_reviewer_response_fallback_on_invalid_json():
    verification = VerificationResult(
        git_diff="", test_output="", tests_passed=True, tests_run=0, tests_failed=0
    )
    result = _parse_reviewer_response("not json", verification)
    assert result.verdict == "pass"  # tests_passed=True → objective verdict


# ── Unit tests: _log_provider_usage ───────────────────────────────────────────

def test_log_provider_usage_records_actual_provider():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE provider_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            task TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_actually_used TEXT NULL,
            sub_task TEXT NOT NULL,
            summary TEXT NULL,
            input_tokens INTEGER NULL,
            output_tokens INTEGER NULL,
            runtime_ms INTEGER NULL
        )
    """)
    _log_provider_usage(
        conn, "my task", "azure_openai", "local", "sub1", "did stuff", 10, 20, 500
    )
    row = conn.execute("SELECT provider, provider_actually_used, input_tokens FROM provider_usage").fetchone()
    assert row[0] == "azure_openai"   # hint
    assert row[1] == "local"           # actually used (fallback)
    assert row[2] == 10


def test_log_provider_usage_noop_when_no_db():
    # Should not raise.
    _log_provider_usage(None, "task", "local", "local", "sub", "summary")


# ── Integration tests: MultiProviderOrchestrator.run ─────────────────────────

@pytest.mark.asyncio
async def test_run_local_provider_sequential(prompts_dir: Path):
    """Full run with local provider; all steps execute in order."""
    router_json = json.dumps({"provider": "local", "role": "developer", "confidence": 0.9, "reasoning": "easy"})
    llama = _make_llama([
        router_json,       # router
        _context_json(),   # context_gatherer
        _plan_json(),      # planner
        _developer_json(), # developer
        _reviewer_json(),  # reviewer
    ])

    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE router_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, task TEXT, decision TEXT, provider TEXT, role TEXT, confidence REAL, reasoning TEXT, consult_query TEXT, sub_tasks_json TEXT, raw_response TEXT);
        CREATE TABLE provider_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, task TEXT NOT NULL, provider TEXT NOT NULL, provider_actually_used TEXT, sub_task TEXT NOT NULL, summary TEXT, input_tokens INTEGER, output_tokens INTEGER, runtime_ms INTEGER);
    """)

    with patch("modules.multi_provider_orchestrator._run_git_diff", return_value=""), \
         patch("modules.multi_provider_orchestrator.subprocess.run") as mock_proc:
        mock_proc.return_value = MagicMock(returncode=0, stdout="10 passed", stderr="")
        orch = MultiProviderOrchestrator(
            llama_client=llama, db=conn, prompts_dir=prompts_dir
        )
        result = await orch.run("Fix a bug")

    assert result.error == ""
    assert result.router_decision.provider == "local"
    assert result.context is not None
    assert result.plan is not None
    assert len(result.developer_results) == 1
    assert result.developer_results[0].provider_used == "local"
    assert result.verification is not None
    assert result.verification.tests_passed is True
    assert result.reviewer_result is not None
    assert result.reviewer_result.verdict == "pass"


@pytest.mark.asyncio
async def test_run_azure_provider_delegates(prompts_dir: Path):
    """When router selects azure_openai, developer step uses azure_run_fn."""
    router_json = json.dumps({"provider": "azure_openai", "role": "developer", "confidence": 0.8, "reasoning": "complex"})
    llama = _make_llama([
        router_json,
        _context_json(),
        _plan_json(sub_tasks=[{"description": "complex task", "provider_hint": "azure_openai"}]),
        _reviewer_json(),
    ])

    azure_fn = AsyncMock(return_value=_developer_json(summary="azure did it"))

    with patch("modules.multi_provider_orchestrator._run_git_diff", return_value=""), \
         patch("modules.multi_provider_orchestrator.subprocess.run") as mock_proc:
        mock_proc.return_value = MagicMock(returncode=0, stdout="5 passed", stderr="")
        orch = MultiProviderOrchestrator(
            llama_client=llama,
            azure_run_fn=azure_fn,
            prompts_dir=prompts_dir,
        )
        result = await orch.run("Complex task")

    assert result.error == ""
    assert len(result.developer_results) == 1
    dev = result.developer_results[0]
    assert dev.provider_used == "azure_openai"
    assert dev.summary == "azure did it"
    azure_fn.assert_called_once()


@pytest.mark.asyncio
async def test_run_codex_provider_delegates(prompts_dir: Path):
    """When router selects codex, developer step uses codex_run_fn."""
    router_json = json.dumps({"provider": "codex", "role": "developer", "confidence": 0.85, "reasoning": "automated"})
    llama = _make_llama([
        router_json,
        _context_json(),
        _plan_json(sub_tasks=[{"description": "automate tests", "provider_hint": "codex"}]),
        _reviewer_json(),
    ])

    codex_fn = AsyncMock(return_value=_developer_json(summary="codex automated it"))

    with patch("modules.multi_provider_orchestrator._run_git_diff", return_value=""), \
         patch("modules.multi_provider_orchestrator.subprocess.run") as mock_proc:
        mock_proc.return_value = MagicMock(returncode=0, stdout="8 passed", stderr="")
        orch = MultiProviderOrchestrator(
            llama_client=llama,
            codex_run_fn=codex_fn,
            prompts_dir=prompts_dir,
        )
        result = await orch.run("Generate tests")

    assert result.error == ""
    dev = result.developer_results[0]
    assert dev.provider_used == "codex"
    codex_fn.assert_called_once()


@pytest.mark.asyncio
async def test_run_codex_fallback_to_local_when_no_fn(prompts_dir: Path):
    """When codex_run_fn is absent, developer falls back to local execution."""
    router_json = json.dumps({"provider": "codex", "role": "developer", "confidence": 0.85, "reasoning": "automated"})
    llama = _make_llama([
        router_json,
        _context_json(),
        _plan_json(sub_tasks=[{"description": "automate tests", "provider_hint": "codex"}]),
        _developer_json(summary="local fallback"),
        _reviewer_json(),
    ])

    with patch("modules.multi_provider_orchestrator._run_git_diff", return_value=""), \
         patch("modules.multi_provider_orchestrator.subprocess.run") as mock_proc:
        mock_proc.return_value = MagicMock(returncode=0, stdout="", stderr="")
        orch = MultiProviderOrchestrator(
            llama_client=llama,
            codex_run_fn=None,  # explicitly absent
            prompts_dir=prompts_dir,
        )
        result = await orch.run("Generate tests")

    assert result.error == ""
    dev = result.developer_results[0]
    assert dev.provider_used == "local"


@pytest.mark.asyncio
async def test_run_verification_failure_sets_reviewer_fail(prompts_dir: Path):
    """When tests fail, reviewer verdict must be fail regardless of LLM output."""
    router_json = json.dumps({"provider": "local", "role": "developer", "confidence": 0.9, "reasoning": "easy"})
    llama = _make_llama([
        router_json,
        _context_json(),
        _plan_json(),
        _developer_json(),
        _reviewer_json(verdict="pass"),  # LLM incorrectly says pass
    ])

    with patch("modules.multi_provider_orchestrator._run_git_diff", return_value="diff --git a/x.py"), \
         patch("modules.multi_provider_orchestrator.subprocess.run") as mock_proc:
        mock_proc.return_value = MagicMock(returncode=1, stdout="2 failed, 8 passed", stderr="")
        orch = MultiProviderOrchestrator(
            llama_client=llama, prompts_dir=prompts_dir
        )
        result = await orch.run("Break something")

    assert result.verification is not None
    assert result.verification.tests_passed is False
    assert result.reviewer_result is not None
    assert result.reviewer_result.verdict == "fail"


@pytest.mark.asyncio
async def test_run_provider_usage_logs_actual_provider(prompts_dir: Path):
    """provider_usage table records the provider actually used, not only the hint."""
    router_json = json.dumps({"provider": "azure_openai", "role": "developer", "confidence": 0.8, "reasoning": "complex"})
    llama = _make_llama([
        router_json,
        _context_json(),
        _plan_json(sub_tasks=[{"description": "subtask", "provider_hint": "azure_openai"}]),
        _reviewer_json(),
    ])
    azure_fn = AsyncMock(return_value=_developer_json())

    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE router_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, task TEXT, decision TEXT, provider TEXT, role TEXT, confidence REAL, reasoning TEXT, consult_query TEXT, sub_tasks_json TEXT, raw_response TEXT);
        CREATE TABLE provider_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, task TEXT NOT NULL, provider TEXT NOT NULL, provider_actually_used TEXT, sub_task TEXT NOT NULL, summary TEXT, input_tokens INTEGER, output_tokens INTEGER, runtime_ms INTEGER);
    """)

    with patch("modules.multi_provider_orchestrator._run_git_diff", return_value=""), \
         patch("modules.multi_provider_orchestrator.subprocess.run") as mock_proc:
        mock_proc.return_value = MagicMock(returncode=0, stdout="5 passed", stderr="")
        orch = MultiProviderOrchestrator(
            llama_client=llama,
            azure_run_fn=azure_fn,
            db=conn,
            prompts_dir=prompts_dir,
        )
        result = await orch.run("My task")

    assert result.error == ""
    row = conn.execute("SELECT provider, provider_actually_used FROM provider_usage").fetchone()
    assert row is not None
    assert row[0] == "azure_openai"   # hint
    assert row[1] == "azure_openai"   # actually used


@pytest.mark.asyncio
async def test_run_stops_on_router_failure(prompts_dir: Path):
    """If the router raises, run returns an OrchestratorResult with error set."""
    llama = _make_llama(["not json at all"])

    orch = MultiProviderOrchestrator(llama_client=llama, prompts_dir=prompts_dir)
    result = await orch.run("anything")

    assert result.error != ""
    assert "Router failed" in result.error


# ── Unit tests: _build_final_response ────────────────────────────────────────

def test_build_final_response_includes_verification():
    decision = RouterDecision(provider="local", role="developer", confidence=0.9, reasoning="easy")
    verification = VerificationResult(
        git_diff="", test_output="10 passed", tests_passed=True, tests_run=10, tests_failed=0
    )
    reviewer = ReviewerResult(verdict="pass", issues=[], suggested_tests=[], summary="all good")
    result = OrchestratorResult(
        task="Fix bug",
        router_decision=decision,
        verification=verification,
        reviewer_result=reviewer,
    )
    text = _build_final_response(result)
    assert "tests_passed=True" in text
    assert "pass" in text


# ── Regression tests: git helpers ─────────────────────────────────────────────

def test_list_tracked_files_returns_file_listing(tmp_path: Path):
    """_list_tracked_files should return a newline-joined list from git ls-files."""
    with patch("modules.multi_provider_orchestrator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="README.md\nagent.py\nmodules/llama_cpp_client.py\n",
        )
        result = _list_tracked_files(str(tmp_path), limit=100)
    assert "README.md" in result
    assert "agent.py" in result
    assert "modules/llama_cpp_client.py" in result


def test_list_tracked_files_truncates_at_limit(tmp_path: Path):
    """_list_tracked_files should honour the limit and append a count suffix."""
    with patch("modules.multi_provider_orchestrator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="\n".join(f"file{i}.py" for i in range(20)),
        )
        result = _list_tracked_files(str(tmp_path), limit=5)
    lines = result.splitlines()
    # First 5 real entries, then a "... (N more)" suffix line
    assert len(lines) == 6
    assert "more" in lines[-1]


def test_run_git_log_returns_commit_summary(tmp_path: Path):
    """_run_git_log should return oneline log output."""
    with patch("modules.multi_provider_orchestrator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc1234 Initial commit\ndef5678 Add feature\n",
        )
        result = _run_git_log(str(tmp_path), lines=5)
    assert "abc1234" in result
    assert "def5678" in result


def test_run_git_status_returns_clean_when_no_changes(tmp_path: Path):
    """_run_git_status returns '(clean)' when git status --short produces no output."""
    with patch("modules.multi_provider_orchestrator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = _run_git_status(str(tmp_path))
    assert result == "(clean)"


def test_is_worktree_clean_true_when_no_changes(tmp_path: Path):
    with patch("modules.multi_provider_orchestrator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        assert _is_worktree_clean(str(tmp_path)) is True


def test_is_worktree_clean_false_when_changes_present(tmp_path: Path):
    with patch("modules.multi_provider_orchestrator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=" M modified.py\n")
        assert _is_worktree_clean(str(tmp_path)) is False


# ── Regression tests: _gather_context on clean worktree ──────────────────────

@pytest.mark.asyncio
async def test_gather_context_clean_worktree_has_repo_evidence(prompts_dir: Path, tmp_path: Path):
    """On a clean worktree (empty diff) the context package must still contain
    repo evidence from the tracked-file listing and git log.  The model should
    never be able to infer 'repository empty' just because the diff is empty.
    """
    model_json = json.dumps({
        "relevant_files": ["README.md", "agent.py"],
        "git_diff_summary": "no uncommitted changes",
        "state_summary": "repository is active",
        "context_package": "Repo contains agent.py and README.md. No uncommitted changes.",
    })
    llama = _make_llama([model_json])

    with patch("modules.multi_provider_orchestrator._run_git_diff", return_value=""), \
         patch("modules.multi_provider_orchestrator._list_tracked_files",
               return_value="README.md\nagent.py\nmodules/llama_cpp_client.py"), \
         patch("modules.multi_provider_orchestrator._run_git_log",
               return_value="abc1234 Initial commit"), \
         patch("modules.multi_provider_orchestrator._run_git_status", return_value="(clean)"):
        orch = MultiProviderOrchestrator(
            llama_client=llama, prompts_dir=prompts_dir, repo_root=str(tmp_path)
        )
        ctx = await orch._gather_context("Give a short status summary of this repository.")

    # The context package must reference the repository, not claim it is empty.
    assert ctx.relevant_files == ["README.md", "agent.py"]
    assert "agent.py" in ctx.context_package or "Repo contains" in ctx.context_package
    # Diff summary should reflect that no uncommitted changes exist, not that repo is empty.
    assert "empty" not in ctx.git_diff_summary.lower()


@pytest.mark.asyncio
async def test_gather_context_fallback_includes_repo_files(prompts_dir: Path, tmp_path: Path):
    """When the model returns invalid JSON, the fallback context must include the
    tracked-file listing so downstream providers have repo evidence.
    """
    llama = _make_llama(["this is not json at all"])

    with patch("modules.multi_provider_orchestrator._run_git_diff", return_value=""), \
         patch("modules.multi_provider_orchestrator._list_tracked_files",
               return_value="README.md\nagent.py"), \
         patch("modules.multi_provider_orchestrator._run_git_log",
               return_value="abc1234 Initial commit"), \
         patch("modules.multi_provider_orchestrator._run_git_status", return_value="(clean)"):
        orch = MultiProviderOrchestrator(
            llama_client=llama, prompts_dir=prompts_dir, repo_root=str(tmp_path)
        )
        ctx = await orch._gather_context("Give a short status summary.")

    # Fallback context_package must contain file listing evidence.
    assert "README.md" in ctx.context_package or "agent.py" in ctx.context_package


# ── Regression tests: _verify task-aware verification ────────────────────────

def test_verify_read_only_task_skips_test_suite():
    """When git diff is empty and no files were changed, the test suite must be
    skipped with an explicit reason; tests_passed must reflect worktree cleanness.
    """
    orch = MultiProviderOrchestrator(repo_root="/tmp/fakerepo")

    with patch("modules.multi_provider_orchestrator._run_git_diff", return_value=""), \
         patch("modules.multi_provider_orchestrator._is_worktree_clean", return_value=True):
        result = orch._verify(files_changed=[])

    assert result.tests_run == 0
    assert result.tests_failed == 0
    assert result.tests_passed is True
    assert "skipped" in result.test_output.lower()
    assert "read-only" in result.test_output.lower()


def test_verify_read_only_task_fails_when_worktree_unexpectedly_dirty():
    """A read-only task that leaves the worktree dirty should report failure."""
    orch = MultiProviderOrchestrator(repo_root="/tmp/fakerepo")

    with patch("modules.multi_provider_orchestrator._run_git_diff", return_value=""), \
         patch("modules.multi_provider_orchestrator._is_worktree_clean", return_value=False):
        result = orch._verify(files_changed=None)

    assert result.tests_passed is False
    assert "dirty" in result.error.lower()


def test_verify_code_changing_task_runs_test_suite():
    """When developer_results report changed files the full test suite must run."""
    orch = MultiProviderOrchestrator(
        repo_root="/tmp/fakerepo",
        test_cmd=["python", "-m", "pytest", "tests/", "-q"],
    )

    with patch("modules.multi_provider_orchestrator._run_git_diff", return_value=""), \
         patch("modules.multi_provider_orchestrator.subprocess.run") as mock_proc:
        mock_proc.return_value = MagicMock(returncode=0, stdout="7 passed", stderr="")
        result = orch._verify(files_changed=["module.py"])

    assert result.tests_passed is True
    assert result.tests_run == 7
    mock_proc.assert_called_once()


def test_verify_git_diff_triggers_test_suite_even_without_reported_files():
    """Even if developer_results is empty, a non-empty git diff must trigger tests."""
    orch = MultiProviderOrchestrator(
        repo_root="/tmp/fakerepo",
        test_cmd=["python", "-m", "pytest", "tests/", "-q"],
    )

    with patch("modules.multi_provider_orchestrator._run_git_diff",
               return_value="diff --git a/x.py b/x.py\n..."), \
         patch("modules.multi_provider_orchestrator.subprocess.run") as mock_proc:
        mock_proc.return_value = MagicMock(returncode=1, stdout="1 failed", stderr="")
        result = orch._verify(files_changed=[])

    assert result.tests_passed is False
    assert result.tests_failed == 1
    mock_proc.assert_called_once()


@pytest.mark.asyncio
async def test_run_read_only_task_skips_test_suite(prompts_dir: Path):
    """Full pipeline test: a read-only status task must not run the test suite."""
    router_json = json.dumps({
        "provider": "local", "role": "developer", "confidence": 1.0, "reasoning": "read-only"
    })
    # Developer reports no file changes for the read-only task.
    dev_json = json.dumps({"files_changed": [], "summary": "status summary", "notes": ""})
    llama = _make_llama([
        router_json,
        _context_json(),
        _plan_json(sub_tasks=[{"description": "summarise repo", "provider_hint": "local"}]),
        dev_json,
        _reviewer_json(),
    ])

    with patch("modules.multi_provider_orchestrator._run_git_diff", return_value=""), \
         patch("modules.multi_provider_orchestrator._list_tracked_files",
               return_value="README.md\nagent.py"), \
         patch("modules.multi_provider_orchestrator._run_git_log",
               return_value="abc1234 Init"), \
         patch("modules.multi_provider_orchestrator._run_git_status", return_value="(clean)"), \
         patch("modules.multi_provider_orchestrator._is_worktree_clean", return_value=True):
        orch = MultiProviderOrchestrator(llama_client=llama, prompts_dir=prompts_dir)
        result = await orch.run("Give a short status summary of this repository.")

    assert result.error == ""
    assert result.verification is not None
    assert result.verification.tests_run == 0
    assert "skipped" in result.verification.test_output.lower()
    # Test suite subprocess must NOT have been called.
    assert result.verification.tests_passed is True
