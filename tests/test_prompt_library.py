"""Tests for modules/prompt_library.py."""

import textwrap
from pathlib import Path

import pytest

from modules.prompt_library import (
    PromptLibraryError,
    get_prompt_metadata,
    load_prompt,
    render_prompt,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def prompts_dir(tmp_path: Path) -> Path:
    """Create a minimal set of prompt files in a temp directory."""
    content = textwrap.dedent("""\
        ---
        version: "1.0"
        role: router
        ---
        # Router Prompt
        Decide: {{task}}
    """)
    (tmp_path / "router_prompt.md").write_text(content)

    planner = textwrap.dedent("""\
        ---
        version: "1.0"
        role: planner
        ---
        Plan this: {{task}}
    """)
    (tmp_path / "planner_prompt.md").write_text(planner)

    developer = textwrap.dedent("""\
        ---
        version: "1.0"
        role: developer
        ---
        Develop: {{sub_task}} with context {{context}}
    """)
    (tmp_path / "developer_prompt.md").write_text(developer)

    tester = textwrap.dedent("""\
        ---
        version: "1.0"
        role: tester
        ---
        Test: {{developer_output}} files {{files_changed}}
    """)
    (tmp_path / "tester_prompt.md").write_text(tester)

    return tmp_path


# ── load_prompt ───────────────────────────────────────────────────────────────

def test_load_prompt_strips_front_matter(prompts_dir):
    body = load_prompt("router", prompts_dir=prompts_dir)
    assert "---" not in body
    assert "# Router Prompt" in body
    assert "{{task}}" in body


def test_load_prompt_unknown_role_raises():
    with pytest.raises(PromptLibraryError, match="Unknown prompt role"):
        load_prompt("nonexistent")


def test_load_prompt_missing_file_raises(tmp_path):
    with pytest.raises(PromptLibraryError, match="not found"):
        load_prompt("router", prompts_dir=tmp_path)


# ── render_prompt ─────────────────────────────────────────────────────────────

def test_render_prompt_substitutes_variables(prompts_dir):
    rendered = render_prompt("router", {"task": "Fix the bug"}, prompts_dir=prompts_dir)
    assert "Fix the bug" in rendered
    assert "{{task}}" not in rendered


def test_render_prompt_missing_variable_raises(prompts_dir):
    with pytest.raises(PromptLibraryError, match="Missing template variables"):
        render_prompt("router", {}, prompts_dir=prompts_dir)


def test_render_prompt_extra_variables_ignored(prompts_dir):
    rendered = render_prompt(
        "router",
        {"task": "Hello", "extra": "ignored"},
        prompts_dir=prompts_dir,
    )
    assert "Hello" in rendered


# ── get_prompt_metadata ───────────────────────────────────────────────────────

def test_get_prompt_metadata_returns_front_matter(prompts_dir):
    meta = get_prompt_metadata("router", prompts_dir=prompts_dir)
    assert meta["version"] == "1.0"
    assert meta["role"] == "router"


def test_get_prompt_metadata_unknown_role_raises():
    with pytest.raises(PromptLibraryError, match="Unknown prompt role"):
        get_prompt_metadata("unknown")
