from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

from modules.local_file_localizer import _prompt_budget, add_likely_files, localize, repo_map
from modules.run_store import init_db
from scripts.localization_replay import candidate_commits, list_localizations, replay, replay_commit


class FakeLlama:
    timeout = 300

    def __init__(self, response: str):
        self.response = response
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def test_invalid_duplicate_and_cap(tmp_path: Path):
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("pass\n")
    client = FakeLlama('{"paths":["a.py","missing.py","a.py","b.py","c.py"]}')
    assert asyncio.run(localize("issue", tmp_path, client, k=2)) == ["a.py", "b.py"]
    assert client.calls[0]["max_tokens"] <= 150
    assert client.calls[0]["response_format"]["type"] == "json_schema"


def test_malformed_and_bounded_map(tmp_path: Path):
    (tmp_path / "long.py").write_text('"""' + "x" * 200 + '"""\n')
    (tmp_path / "short.py").write_text("def short(): pass\n")
    assert asyncio.run(localize("issue", tmp_path, FakeLlama("not json"))) == []
    assert len(repo_map(tmp_path, token_budget=5).encode()) <= 20


def test_flag_off_is_noop(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("NEXUS_LOCAL_LOCALIZE", raising=False)
    client = FakeLlama('{"paths":["a.py"]}')
    assert add_likely_files("unchanged", tmp_path, client) == "unchanged"
    assert client.calls == []


def test_map_budget_uses_45_seconds_and_not_client_timeout(tmp_path: Path):
    client = FakeLlama('{"paths":[]}')
    client.timeout = 9999
    with patch("modules.local_file_localizer.repo_map", return_value="") as mapping:
        asyncio.run(localize("issue", tmp_path, client, rates=(7.0, 2.6)))
    overhead = max(64, len(b"issue") // 4 + 90)
    assert mapping.call_args.kwargs["token_budget"] == max(
        0, _prompt_budget(45, (7.0, 2.6)) - overhead,
    )


def test_map_budget_uses_passed_rate_medians(tmp_path: Path):
    with patch("modules.local_file_localizer.repo_map", return_value="") as mapping:
        asyncio.run(localize(
            "issue", tmp_path, FakeLlama('{"paths":[]}'), rates=(70.0, 26.0),
        ))
    overhead = max(64, len(b"issue") // 4 + 90)
    assert mapping.call_args.kwargs["token_budget"] == max(
        0, _prompt_budget(45, (70.0, 26.0)) - overhead,
    )


def test_replay_scores_fixture_commit_and_stores(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "a.py").write_text("old\n")
    (repo / "b.py").write_text("old\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    (repo / "a.py").write_text("new\n")
    (repo / "b.py").write_text("new\n")
    subprocess.run(["git", "commit", "-qam", "fix #696"], cwd=repo, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    db = str(tmp_path / "runs.db")
    row = asyncio.run(replay_commit(
        sha, "fix files", repo, FakeLlama('{"paths":["a.py","wrong.py"]}'),
        issue_number=696, db_path=db,
    ))
    assert row["recall_at_5"] == 0.5
    assert row["precision_at_5"] == 1.0
    assert list_localizations(db_path=db)[0]["commit_sha"] == sha


def test_test_only_commit_is_skipped_and_not_stored(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("old\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    (repo / "tests" / "test_x.py").write_text("new\n")
    subprocess.run(["git", "commit", "-qam", "tests only"], cwd=repo, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    db = str(tmp_path / "runs.db")
    assert asyncio.run(replay_commit(sha, "tests", repo, FakeLlama('{"paths":[]}'), db_path=db)) is None
    assert list_localizations(db_path=db) == []


def test_replay_skips_error_and_keeps_going(tmp_path: Path):
    candidates = [("bad", "bad", None), ("skip", "skip", None), ("good", "good", 7)]

    async def fake_commit(sha, *_args, **_kwargs):
        if sha == "bad":
            raise subprocess.CalledProcessError(1, ["git", "worktree"])
        if sha == "skip":
            return None
        return {"commit_sha": sha}

    with patch("scripts.localization_replay.candidate_commits", return_value=candidates), \
            patch("scripts.localization_replay.replay_commit", side_effect=fake_commit):
        assert asyncio.run(replay(tmp_path, limit=1, client=FakeLlama("{}"))) == [
            {"commit_sha": "good"},
        ]


def test_linked_issue_text_replaces_message_and_pr_suffix_is_not_issue(tmp_path: Path):
    log = (
        "a\x00Fix bug\n\nFixes #7\x00\x1e"
        "b\x00Refactor only (#99)\x00\x1e"
    )
    with patch("scripts.localization_replay._git", return_value=log):
        rows = candidate_commits(
            tmp_path,
            issue_fetcher=lambda number: {"number": number, "title": "Issue title", "body": "Issue body"},
        )
    assert rows == [
        ("a", "Issue title\n\nIssue body", 7),
        ("b", "Refactor only (#99)", None),
    ]


def test_malformed_stored_json_reads_as_empty_lists(tmp_path: Path):
    db = str(tmp_path / "runs.db")
    conn = init_db(db)
    conn.execute(
        """INSERT INTO local_localizations
           (created_at, commit_sha, predicted_json, actual_json, recall_at_5, precision_at_5, seconds)
           VALUES ('now', 'bad-json', '{', 'null', 0, 0, 1)"""
    )
    conn.commit()
    conn.close()
    row = list_localizations(db_path=db)[0]
    assert row["predicted"] == []
    assert row["actual"] == []
