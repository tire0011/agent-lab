"""Issue #246: one real controller turn per scenario, nothing executed.

Scenarios, fixtures and verdicts come from scripts/check_issue_246_controller_role.py,
which keeps working on its own; this is the same check on the shared live-test
base.
"""

from __future__ import annotations

import pytest

from scripts.check_issue_246_controller_role import (
    ISSUE_238_OBSERVATION,
    MEMORY_SEARCH_OBSERVATION,
    TASK,
    verdict,
)

SCENARIOS = {
    "initial": [],
    "after-read": [ISSUE_238_OBSERVATION],
    "after-memory": [ISSUE_238_OBSERVATION, MEMORY_SEARCH_OBSERVATION],
}


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_controller_role(scenario, controller_turn, live_result):
    decision = controller_turn(TASK, SCENARIOS[scenario])
    ok, reason = verdict(scenario, decision)
    live_result.update(scenario=scenario, reason=reason)
    assert ok, reason
