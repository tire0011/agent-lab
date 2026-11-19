"""Open Nexus PRs whose issue is closed are closed with the issue (split parts
done, owner closed it), so finished work does not leave PRs behind."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.nexus_initiative import NexusInitiativeService


def _service(tmp_path: Path, pulls, closer):
    return NexusInitiativeService(
        db_path=str(tmp_path / "initiative.db"),
        experiment_service=SimpleNamespace(repo_root=str(tmp_path)),
        controller=SimpleNamespace(handle_issue=MagicMock()),
        issue_lister=lambda: [{"number": 10, "title": "open", "body": ""}],
        pr_lister=lambda: pulls,
        stale_pr_closer=closer,
        reflection_sweep=lambda **kwargs: None,
    )


def test_closes_only_nexus_prs_of_issues_that_are_not_open(tmp_path: Path):
    pulls = [
        {"number": 1, "headRefName": "nexus/10-still-open"},
        {"number": 2, "headRefName": "nexus/7-done-in-parts"},
        {"number": 3, "headRefName": "nexus-communication"},
        {"number": 4, "headRefName": "vibe/manual-work"},
    ]
    closer = MagicMock(return_value=True)
    service = _service(tmp_path, pulls, closer)
    try:
        service._issue_candidates()
    finally:
        service.close()
    assert [(c.args[0], c.args[1]) for c in closer.call_args_list] == [(2, 7)]
    assert "Issue #7 is closed" in closer.call_args.args[2]


def test_lookup_or_close_failures_do_not_break_candidates(tmp_path: Path):
    def broken():
        raise RuntimeError("gh down")

    service = _service(tmp_path, [], MagicMock())
    service._pr_lister = broken
    try:
        service._issue_candidates()  # must not raise
        service._pr_lister = lambda: [{"number": 2, "headRefName": "nexus/7-x"}]
        service._stale_pr_closer = MagicMock(side_effect=RuntimeError("422"))
        service._issue_candidates()
    finally:
        service.close()


def test_empty_issue_list_closes_nothing(tmp_path: Path):
    closer = MagicMock(return_value=True)
    service = _service(tmp_path, [{"number": 2, "headRefName": "nexus/7-x"}], closer)
    service._issue_lister = lambda: []
    try:
        service._issue_candidates()
    finally:
        service.close()
    closer.assert_not_called()


def test_closer_confirms_the_issue_state(monkeypatch):
    import nexus_tick

    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='{"state": "OPEN"}', stderr="")

    monkeypatch.setattr(nexus_tick.subprocess, "run", run)
    assert nexus_tick._close_stale_pull_request(".", 2, 7, "text") is False
    assert calls == [["gh", "issue", "view", "7", "--json", "state"]]
