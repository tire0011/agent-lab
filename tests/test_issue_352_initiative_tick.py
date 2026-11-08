"""Regression coverage for autonomous issue planning (#352)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import nexus_tick


def test_open_issue_discovery_is_not_label_gated(tmp_path):
    completed = SimpleNamespace(
        returncode=0,
        stdout=json.dumps([{"number": 7, "title": "Plan me", "body": "Do it"}]),
        stderr="",
    )

    with patch("nexus_tick.subprocess.run", return_value=completed) as run:
        issues = nexus_tick._list_open_issues(str(tmp_path))

    assert issues == [{"number": 7, "title": "Plan me", "body": "Do it"}]
    command = run.call_args.args[0]
    assert command[:4] == ["gh", "issue", "list", "--state"]
    assert "--label" not in command
    assert command[command.index("--limit") + 1] == "100"


def test_production_tick_enables_issue_controller():
    unit = Path("deploy/nexus-tick.service").read_text(encoding="utf-8")

    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert exec_start == (
        "ExecStart=/agent/.venv/bin/python /agent/nexus_tick.py --daily-budget 5"
    )
    assert "--integration-only" not in exec_start
