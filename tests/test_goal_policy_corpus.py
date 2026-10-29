"""Regression corpus for the goal policy (#398).

``accept/`` holds verbatim goal texts of issues that were wrongly flagged as a
secret action; ``reject/`` holds goals that really are one. A new false
positive goes into ``accept/`` (see ``scripts/goal-policy-check.py``), and this
test shows whether the fix breaks an earlier case or a real detection.

Since #412 a secret-action match in the goal only warns, so the secret check
is ``_goal_secret_warning``; ``_check_goal_policy`` must still let both pass.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from modules.experiment_service.policy import (
    _check_goal_policy,
    _goal_secret_warning,
    _policy_rule_for_reason,
)

FIXTURES = Path(__file__).parent / "fixtures" / "goal_policy"
ACCEPT = sorted((FIXTURES / "accept").glob("*.md"))
REJECT = sorted((FIXTURES / "reject").glob("*.md"))


def test_corpus_is_not_empty():
    assert {p.stem for p in ACCEPT} >= {"issue_220", "issue_348", "issue_388"}
    assert REJECT


@pytest.mark.parametrize("path", ACCEPT, ids=lambda p: p.stem)
def test_legitimate_issue_is_accepted(path: Path):
    goal = path.read_text(encoding="utf-8")
    assert _check_goal_policy(goal) is None
    assert _goal_secret_warning(goal) is None


@pytest.mark.parametrize("path", REJECT, ids=lambda p: p.stem)
def test_real_secret_action_is_flagged(path: Path):
    goal = path.read_text(encoding="utf-8")
    reason = _goal_secret_warning(goal)
    assert _policy_rule_for_reason(reason) == "secret_action", reason


@pytest.mark.parametrize(
    "goal",
    [
        "Redact the output before the command prints the api key.",
        "Mask stdout so the worker never logs the access token.",
        "Tests cover cases where a command prints a secret, e.g. printing the api key to stdout.",
        "Write `****` instead of printing the api key.",
        "Filter the result so that printing the api key is not possible.",
    ],
)
def test_descriptive_cues_are_mentions(goal: str):
    assert _goal_secret_warning(goal) is None


@pytest.mark.parametrize(
    "goal",
    [
        "Redaction is done. Now print the api key to the log.",
        "Print the api key to the log, then mask the rest of the output.",
        "The redactor works. Read the access token and print it.",
    ],
)
def test_cue_in_another_sentence_or_after_the_match_does_not_exempt(goal: str):
    assert _goal_secret_warning(goal) is not None


def test_plan_check_does_not_flag_env_var_names():
    """Env-var secret names are flagged in goals only; plans name them legitimately."""
    from modules.experiment_service.policy import _SECRET_ACTION_REGEXES

    text = "api.py reads agent_api_key from the environment"
    assert not any(regex.search(text) for regex in _SECRET_ACTION_REGEXES)
    assert _goal_secret_warning(text) is not None


def _load_check_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "goal-policy-check.py"
    spec = importlib.util.spec_from_file_location("goal_policy_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_check_script_builds_the_fixture_goal_text():
    script = _load_check_script()
    fixture = (FIXTURES / "accept" / "issue_348.md").read_text(encoding="utf-8")
    _, header, body = fixture.split("\n\n", 2)
    title = header.split(": ", 1)[1]
    assert script.goal_text(348, title, body) == fixture


def test_check_script_prints_the_matched_span(capsys):
    script = _load_check_script()
    goal = script.goal_text(1, "Debug", "Print the AZURE_OPENAI_API_KEY to the log.")
    assert script.report(goal) == 1
    out = capsys.readouterr().out
    assert "verdict: warning (secret_action)" in out
    assert "[[print the azure_openai_api_key]]" in out
    assert script.report(script.goal_text(1, "Docs", "Fix a typo.")) == 0
