from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from modules.local_file_localizer import add_likely_files, localize, repo_map
from scripts.localization_replay import list_localizations, replay_commit


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
