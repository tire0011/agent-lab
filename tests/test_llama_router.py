"""Tests for modules/llama_router.py."""

import json
import sqlite3
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from modules.llama_cpp_client import LlamaCppClientError
from modules.llama_router import (
    LlamaRouter,
    RouterDecision,
    VALID_PROVIDERS,
    VALID_ROLES,
    _ensure_router_tables,
    _parse_router_response,
)


# ── _parse_router_response ────────────────────────────────────────────────────

def test_parse_router_response_legacy_self():
    raw = json.dumps(
        {"decision": "SELF", "confidence": 0.9, "reasoning": "Simple task."}
    )
    d = _parse_router_response(raw)
    assert d.decision == "SELF"
    assert d.provider == "local"
    assert d.confidence == 0.9
    assert d.reasoning == "Simple task."


def test_parse_router_response_legacy_azure():
    raw = json.dumps(
        {"decision": "AZURE_OPENAI", "confidence": 0.7, "reasoning": "Complex."}
    )
    d = _parse_router_response(raw)
    assert d.decision == "AZURE_OPENAI"
    assert d.provider == "azure_openai"


def test_parse_router_response_new_format_provider_role():
    raw = json.dumps(
        {"provider": "codex", "role": "developer", "confidence": 0.85, "reasoning": "Automated."}
    )
    d = _parse_router_response(raw)
    assert d.provider == "codex"
    assert d.role == "developer"
    assert d.decision == "CODEX"


def test_parse_router_response_with_sub_tasks():
    raw = json.dumps(
        {
            "provider": "codex",
            "role": "developer",
            "confidence": 0.8,
            "reasoning": "Needs code change.",
            "sub_tasks": ["task1", "task2"],
        }
    )
    d = _parse_router_response(raw)
    assert d.sub_tasks == ["task1", "task2"]


def test_parse_router_response_unknown_decision_raises():
    raw = json.dumps({"decision": "UNKNOWN", "confidence": 0.5, "reasoning": "?"})
    with pytest.raises(LlamaCppClientError, match="unknown decision"):
        _parse_router_response(raw)


# ── RouterDecision.is_valid ───────────────────────────────────────────────────

def test_router_decision_is_valid():
    for provider in ("local", "azure_openai", "codex", "copilot"):
        for role in ("developer", "planner", "reviewer", "context_gatherer"):
            d = RouterDecision(provider=provider, role=role, confidence=1.0, reasoning="ok")
            assert d.is_valid(), f"Expected valid for provider={provider} role={role}"


def test_router_decision_is_invalid():
    d = RouterDecision(provider="unknown_provider", role="developer", confidence=0.0, reasoning="")
    assert not d.is_valid()


# ── _ensure_router_tables ─────────────────────────────────────────────────────

def test_ensure_router_tables_creates_table():
    conn = sqlite3.connect(":memory:")
    _ensure_router_tables(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "router_decisions" in tables
    conn.close()


def test_ensure_router_tables_idempotent():
    conn = sqlite3.connect(":memory:")
    _ensure_router_tables(conn)
    _ensure_router_tables(conn)  # should not raise
    conn.close()


# ── LlamaRouter.route ─────────────────────────────────────────────────────────

@pytest.fixture()
def prompts_dir(tmp_path: Path) -> Path:
    content = textwrap.dedent("""\
        ---
        version: "1.0"
        role: router
        ---
        # Router
        Task: {{task}}
    """)
    (tmp_path / "router_prompt.md").write_text(content)
    return tmp_path


@pytest.mark.asyncio
async def test_router_route_returns_decision(prompts_dir):
    mock_client = MagicMock()
    mock_client.complete = AsyncMock(
        return_value=json.dumps(
            {"decision": "SELF", "confidence": 0.95, "reasoning": "Easy."}
        )
    )

    conn = sqlite3.connect(":memory:")
    router = LlamaRouter(client=mock_client, db=conn, prompts_dir=prompts_dir)
    decision = await router.route("Fix a typo")

    assert decision.decision == "SELF"
    assert decision.confidence == 0.95
    conn.close()


@pytest.mark.asyncio
async def test_router_route_persists_to_db(prompts_dir):
    mock_client = MagicMock()
    mock_client.complete = AsyncMock(
        return_value=json.dumps(
            {"decision": "AZURE_OPENAI", "confidence": 0.6, "reasoning": "Hard."}
        )
    )

    conn = sqlite3.connect(":memory:")
    router = LlamaRouter(client=mock_client, db=conn, prompts_dir=prompts_dir)
    await router.route("Implement ML pipeline")

    rows = conn.execute("SELECT decision FROM router_decisions").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "AZURE_OPENAI"
    conn.close()
