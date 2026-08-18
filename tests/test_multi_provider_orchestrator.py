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
    _log_provider_usage,
    _parse_developer_response,
    _parse_pytest_summary,
    _parse_reviewer_response,
    _run_git_diff,
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
            version: "1.0"
            role: context_gatherer
            ---
            Task: {{task}}
            Diff: {{git_diff}}
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
