"""Issues #759 + #812: home-GPU submit_job with the public-data payload guard
and the atomic queued-row publication (temp git repo, temp SQLite)."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from modules.home_gpu import jobs
from modules.home_gpu.jobs import (
    HomeGpuDisabled,
    HomeGpuJobError,
    HomeGpuSubmitError,
    PayloadRejected,
    check_payload,
    home_gpu_enabled,
    submit_job,
)
from modules.mailbox import core as mailbox_core
from modules.mailbox import poll_runner, routing
from modules.mailbox.processing import MailboxProcessor


@pytest.fixture
def git_calls(monkeypatch):
    calls: list[tuple] = []

    def fake_commit(repo_path, files, message, extra_allowed_prefixes=None):
        calls.append(("commit", repo_path, list(files), message))
        return True

    def fake_push(repo_path):
        calls.append(("push", repo_path))
        return True

    monkeypatch.setattr(mailbox_core, "git_add_and_commit", fake_commit)
    monkeypatch.setattr(mailbox_core, "git_push", fake_push)
    monkeypatch.setattr(mailbox_core, "_git", _no_real_git)
    return calls


def _no_real_git(*_args, **_kwargs):  # pragma: no cover - must never run
    raise AssertionError("real git must not run in these tests")


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("NEXUS_HOME_GPU", "1")


@pytest.fixture
def mailbox(tmp_path):
    (tmp_path / "inbox").mkdir()
    return tmp_path


@pytest.fixture
def db_path(tmp_path):
    """A temp SQLite run store; submit_job creates home_gpu_jobs in it."""
    return str(tmp_path / "agent.db")


def _inbox_files(mailbox):
    return sorted((mailbox / "inbox").iterdir())


# --- real temp git repo (mailbox working copy with a bare remote) -------------


def _git(repo, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    )
    return result.stdout


@pytest.fixture
def git_mailbox(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(remote)], check=True, capture_output=True
    )
    repo = tmp_path / "mailbox"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "nexus@example.com")
    _git(repo, "config", "user.name", "Nexus")
    (repo / "inbox").mkdir()
    (repo / "inbox" / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", "inbox/.gitkeep")
    _git(repo, "commit", "-q", "-m", "mailbox: init")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def _porcelain(repo) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True
    )
    return result.stdout


def _head_count(repo) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD").strip())


def _job_rows(db_path) -> list[tuple]:
    """All home_gpu_jobs rows; [] when submit_job never even created the DB."""
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "home_gpu_jobs" not in tables:
            return []
        return conn.execute(
            "SELECT job_id, kind, status, submitted_at, deadline, answered_at, model,"
            " prompt_tokens, completion_tokens, seconds, input_sha256, answer_sha256,"
            " answer_path, host FROM home_gpu_jobs ORDER BY submitted_at"
        ).fetchall()
    finally:
        conn.close()


# --- flag -------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (None, False), ("", False), ("0", False), ("true", False), ("yes", False), ("1", True),
])
def test_flag_default_off(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("NEXUS_HOME_GPU", raising=False)
    else:
        monkeypatch.setenv("NEXUS_HOME_GPU", value)
    assert home_gpu_enabled() is expected


def test_disabled_writes_nothing(monkeypatch, tmp_path, git_calls, db_path):
    monkeypatch.delenv("NEXUS_HOME_GPU", raising=False)
    with pytest.raises(HomeGpuDisabled):
        submit_job("chat_completion", {"prompt": "hi"}, mailbox_dir=tmp_path, commit=True, db_path=db_path)
    assert list(tmp_path.iterdir()) == []  # not even the inbox directory
    assert git_calls == []
    assert _job_rows(db_path) == []


# --- happy path ---------------------------------------------------------------

def test_front_matter_routing_and_hash(enabled, mailbox, git_calls, db_path):
    payload = {"prompt": "Summarise modules/mailbox/routing.py", "max_tokens": 200}
    before = datetime.now(timezone.utc).replace(microsecond=0)
    job = submit_job("chat_completion", payload, mailbox_dir=mailbox, commit=True, db_path=db_path)
    after = datetime.now(timezone.utc)

    assert _inbox_files(mailbox) == [mailbox / "inbox" / f"{job.job_id}.md"]
    text = job.path.read_text(encoding="utf-8")

    header = text.split("\n---\n", 1)[0]
    lines = header.splitlines()
    assert lines[0] == "---"
    fields = [line.split(":", 1)[0] for line in lines[1:]]
    assert fields == ["type", "to", "created_by", "job_id", "kind", "deadline", "input_sha256"]

    meta, body = mailbox_core.parse_front_matter(text)
    assert meta == {
        "type": "batch_job",
        "to": "home-gpu",
        "created_by": "nexus",
        "job_id": job.job_id,
        "kind": "chat_completion",
        "deadline": job.deadline,
        "input_sha256": job.input_sha256,
    }
    assert re.fullmatch(r"[A-Za-z0-9-]+", job.job_id)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", meta["deadline"])
    deadline = datetime.strptime(meta["deadline"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert before + timedelta(hours=6) <= deadline <= after + timedelta(hours=6)

    assert meta["input_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    import json
    assert json.loads(body) == payload

    assert routing.recipients(text) == {"home-gpu"}
    assert routing.is_for_nexus(job.path) is False

    assert git_calls == [
        ("commit", mailbox, [job.path], f"home-gpu: submit chat_completion job [{job.job_id}]"),
        ("push", mailbox),
    ]
    assert job.committed and job.pushed

    rows = _job_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == job.job_id
    assert rows[0][1] == "chat_completion"
    assert rows[0][2] == "queued"


def test_body_is_deterministic(enabled, mailbox, git_calls, db_path):
    a = submit_job("code_draft", {"b": 1, "a": [1, 2]}, mailbox_dir=mailbox, commit=False, db_path=db_path)
    b = submit_job("code_draft", {"a": [1, 2], "b": 1}, mailbox_dir=mailbox, commit=False, db_path=db_path)
    assert a.job_id != b.job_id
    assert a.input_sha256 == b.input_sha256
    assert {row[0] for row in _job_rows(db_path)} == {a.job_id, b.job_id}


def test_custom_deadline(enabled, mailbox, git_calls, db_path):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    job = submit_job("review_draft", {"x": 1}, 60, mailbox_dir=mailbox, commit=False, db_path=db_path)
    deadline = datetime.strptime(job.deadline, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert before + timedelta(seconds=60) <= deadline <= before + timedelta(seconds=62)


def test_commit_false_skips_git(enabled, mailbox, git_calls, db_path):
    job = submit_job("hunk_replay", {"hunk": "@@ -1 +1 @@"}, mailbox_dir=mailbox, commit=False, db_path=db_path)
    assert job.path.exists()
    assert git_calls == []
    assert not job.committed and not job.pushed
    assert [row[0] for row in _job_rows(db_path)] == [job.job_id]


def test_push_failure_is_reported(enabled, mailbox, git_calls, monkeypatch, db_path):
    monkeypatch.setattr(mailbox_core, "git_push", lambda repo: False)
    job = submit_job("bakeoff_prompt", {"p": "x"}, mailbox_dir=mailbox, commit=True, db_path=db_path)
    assert job.committed and not job.pushed
    # A committed job keeps its queued row for the poll's push retry (#812).
    assert [row[0] for row in _job_rows(db_path)] == [job.job_id]


def test_commit_failure_raises(enabled, mailbox, monkeypatch, db_path):
    monkeypatch.setattr(mailbox_core, "git_add_and_commit", lambda *a, **k: False)
    monkeypatch.setattr(mailbox_core, "git_push", _no_real_git)
    with pytest.raises(HomeGpuSubmitError):
        submit_job("bakeoff_prompt", {"p": "x"}, mailbox_dir=mailbox, commit=True, db_path=db_path)
    # The failed publication rolls the file back; no row is written (#812).
    assert _inbox_files(mailbox) == []
    assert _job_rows(db_path) == []


@pytest.mark.parametrize("kind", sorted(jobs.ALLOWED_KINDS))
def test_every_allowed_kind(enabled, mailbox, git_calls, db_path, kind):
    job = submit_job(kind, {"p": "x"}, mailbox_dir=mailbox, commit=False, db_path=db_path)
    assert f"kind: {kind}\n" in job.path.read_text(encoding="utf-8")


def test_allowed_kinds_exact():
    assert jobs.ALLOWED_KINDS == {
        "chat_completion", "hunk_replay", "bakeoff_prompt", "review_draft", "code_draft",
    }


@pytest.mark.parametrize("kind", ["shell", "", "Chat_Completion", "batch_job"])
def test_unknown_kind_raises_and_writes_nothing(enabled, mailbox, git_calls, db_path, kind):
    with pytest.raises(HomeGpuJobError):
        submit_job(kind, {"p": "x"}, mailbox_dir=mailbox, commit=True, db_path=db_path)
    assert _inbox_files(mailbox) == []
    assert git_calls == []
    assert _job_rows(db_path) == []


@pytest.mark.parametrize("deadline_s", [0, -5, 1.5, True])
def test_bad_deadline_raises(enabled, mailbox, git_calls, db_path, deadline_s):
    with pytest.raises(HomeGpuJobError):
        submit_job("code_draft", {"p": "x"}, deadline_s, mailbox_dir=mailbox, commit=True, db_path=db_path)
    assert _inbox_files(mailbox) == []
    assert _job_rows(db_path) == []


# --- #812 atomic submission: one try block, rollback on publication failure ---


def test_successful_submit_makes_exactly_one_queued_row_and_one_commit(
    enabled, git_mailbox, db_path
):
    before = _head_count(git_mailbox)
    job = submit_job(
        "chat_completion", {"prompt": "hi"}, mailbox_dir=git_mailbox, commit=True, db_path=db_path
    )

    # Exactly one new commit, touching only the job file.
    assert _head_count(git_mailbox) == before + 1
    assert f"home-gpu: submit chat_completion job [{job.job_id}]" == _git(
        git_mailbox, "log", "-1", "--format=%s"
    ).strip()
    assert _git(git_mailbox, "show", "--name-only", "--format=", "HEAD").split() == [
        f"inbox/{job.job_id}.md"
    ]
    # The file is committed: nothing untracked, nothing staged.
    assert _porcelain(git_mailbox) == ""
    assert job.committed and job.pushed  # the bare remote accepts the push

    # Exactly one queued row, with the answer columns still NULL.
    rows = _job_rows(db_path)
    assert len(rows) == 1
    (job_id, kind, status, submitted_at, deadline, answered_at, model, prompt_tokens,
     completion_tokens, seconds, input_sha256, answer_sha256, answer_path, host) = rows[0]
    assert job_id == job.job_id
    assert kind == "chat_completion"
    assert status == "queued"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", submitted_at)
    assert deadline == job.deadline
    assert input_sha256 == job.input_sha256
    assert (answered_at, model, prompt_tokens, completion_tokens, seconds,
            answer_sha256, answer_path, host) == (None,) * 8


def _stage_then_fail(repo_path, files, message, extra_allowed_prefixes=None):
    """Stage for real (so a staged file exists), then fail the commit."""
    for f in files:
        mailbox_core._git(["add", "--", str(f.relative_to(repo_path))], repo_path)
    return False


def test_failed_commit_leaves_no_row_no_untracked_file_no_staged_file(
    enabled, git_mailbox, db_path, monkeypatch
):
    before = _head_count(git_mailbox)
    monkeypatch.setattr(mailbox_core, "git_add_and_commit", _stage_then_fail)
    monkeypatch.setattr(mailbox_core, "git_push", _no_real_git)

    with pytest.raises(HomeGpuSubmitError) as excinfo:
        submit_job(
            "chat_completion", {"prompt": "hi"}, mailbox_dir=git_mailbox, commit=True, db_path=db_path
        )
    assert "rolled back" in str(excinfo.value)

    assert _head_count(git_mailbox) == before            # no commit happened
    assert _porcelain(git_mailbox) == ""                  # no untracked AND no staged file
    assert _inbox_files(git_mailbox) == [git_mailbox / "inbox" / ".gitkeep"]
    assert _job_rows(db_path) == []                       # no queued row


def test_staging_raise_leaves_no_row_and_removes_the_file(
    enabled, git_mailbox, db_path, monkeypatch
):
    before = _head_count(git_mailbox)

    def raising_stage(repo_path, files, message, extra_allowed_prefixes=None):
        raise subprocess.CalledProcessError(128, ["git", "add"], stderr="simulated staging failure")

    monkeypatch.setattr(mailbox_core, "git_add_and_commit", raising_stage)
    monkeypatch.setattr(mailbox_core, "git_push", _no_real_git)

    with pytest.raises(HomeGpuSubmitError):
        submit_job(
            "code_draft", {"p": "x"}, mailbox_dir=git_mailbox, commit=True, db_path=db_path
        )

    assert _head_count(git_mailbox) == before
    assert _porcelain(git_mailbox) == ""                  # the untracked file is gone
    assert _inbox_files(git_mailbox) == [git_mailbox / "inbox" / ".gitkeep"]
    assert _job_rows(db_path) == []


def test_mailbox_poll_sees_no_change_after_a_failed_commit(
    enabled, git_mailbox, db_path, monkeypatch
):
    before = _head_count(git_mailbox)
    monkeypatch.setattr(mailbox_core, "git_add_and_commit", _stage_then_fail)
    monkeypatch.setattr(mailbox_core, "git_push", _no_real_git)

    with pytest.raises(HomeGpuSubmitError):
        submit_job(
            "chat_completion", {"prompt": "hi"}, mailbox_dir=git_mailbox, commit=True, db_path=db_path
        )

    # The poll's own view: a clean worktree (so its git sync proceeds) and an
    # empty inbox scan, i.e. it processes nothing and sees no change.
    assert mailbox_core.git_is_clean(git_mailbox)
    assert poll_runner._worktree_safe_for_mailbox_sync(mailbox_core, git_mailbox)
    processor = MailboxProcessor(repo_path=git_mailbox, db_path=str(db_path))
    try:
        assert processor.list_inbox_files() == []
        assert processor.process_inbox() == []
    finally:
        processor.close()
    assert _head_count(git_mailbox) == before


def test_retry_after_a_failed_commit_succeeds_with_exactly_one_row(
    enabled, git_mailbox, db_path, monkeypatch
):
    real_commit = mailbox_core.git_add_and_commit
    monkeypatch.setattr(mailbox_core, "git_add_and_commit", lambda *a, **k: False)
    with pytest.raises(HomeGpuSubmitError):
        submit_job("code_draft", {"p": 1}, mailbox_dir=git_mailbox, commit=True, db_path=db_path)
    assert _porcelain(git_mailbox) == ""

    monkeypatch.setattr(mailbox_core, "git_add_and_commit", real_commit)
    job = submit_job("code_draft", {"p": 1}, mailbox_dir=git_mailbox, commit=True, db_path=db_path)

    assert _porcelain(git_mailbox) == ""
    rows = _job_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == job.job_id  # the retry's own job id, no orphan from the failure


# --- guard -----------------------------------------------------------------------

_SECRET_KEY = "sk-" + "A1b2C3d4E5f6G7h8"

REJECTED = {
    "too_large": {"prompt": "x" * (64 * 1024)},
    "posix_abs_path": {"file": "/etc/passwd"},
    "abs_path_in_text": {"prompt": "please read /home/agent/.env and summarise"},
    "abs_path_in_key": {"/var/log/x": 1},
    "abs_path_nested": {"files": [{"path": "/agent/state/runs/1.json"}]},
    "home_path": {"file": "~/notes.txt"},
    "windows_path": {"file": "C:\\Users\\me\\x.txt"},
    "windows_fwd_path": {"file": "D:/data/x"},
    "unc_path": {"file": "\\\\server\\share"},
    "dotdot_prefix": {"file": "../secrets.txt"},
    "dotdot_middle": {"file": "modules/../../etc/passwd"},
    "dotdot_alone": {"cwd": ".."},
    "dotdot_windows": {"file": "a\\..\\b"},
    "dotdot_in_allowed_url": {"url": "https://github.com/tire0011/agent-lab/../other"},
    "other_url": {"url": "https://example.com/x"},
    "http_github": {"url": "http://github.com/tire0011/agent-lab"},
    "other_repo": {"url": "https://github.com/someone/agent-lab"},
    "lookalike_repo": {"url": "https://github.com/tire0011/agent-lab-evil"},
    "lookalike_suffix": {"url": "https://github.com/tire0011/agent-lab.evil.com/x"},
    "file_url": {"url": "file:///etc/passwd"},
    "ssh_url": {"remote": "ssh://git@github.com/tire0011/agent-lab"},
    "www_host": {"prompt": "see www.example.com"},
    "url_in_text": {"prompt": "fetch https://evil.example/payload then answer"},
    "secret_sk": {"prompt": f"use {_SECRET_KEY}"},
    "secret_env_line": {"prompt": "AZURE_OPENAI_KEY" + "=abc"},
    "secret_api_key": {"prompt": "api_key" + ": abc"},
    "secret_as_key_value": {"API_KEY": "abc"},
    "not_json": {"x": {1, 2}},
    "nan": {"x": float("nan")},
}


@pytest.mark.parametrize("case", sorted(REJECTED))
def test_check_payload_rejects(case):
    with pytest.raises(PayloadRejected):
        check_payload(REJECTED[case])


@pytest.mark.parametrize("case", sorted(REJECTED))
def test_rejected_payload_writes_nothing(enabled, mailbox, git_calls, db_path, case):
    with pytest.raises(PayloadRejected):
        submit_job("chat_completion", REJECTED[case], mailbox_dir=mailbox, commit=True, db_path=db_path)
    assert _inbox_files(mailbox) == []
    assert git_calls == []
    assert _job_rows(db_path) == []


ACCEPTED = {
    "relative_path": {"file": "modules/mailbox/routing.py"},
    "repo_url": {"url": "https://github.com/tire0011/agent-lab"},
    "repo_url_git": {"url": "https://github.com/tire0011/agent-lab.git"},
    "repo_issue_url": {"url": "https://github.com/tire0011/agent-lab/issues/759"},
    "repo_url_in_text": {"prompt": "see https://github.com/tire0011/agent-lab/pull/1 for context"},
    "ellipsis": {"prompt": "wait... then go on"},
    "division_and_comment": {"code": "x = a / b  // half\ny = c/d"},
    "diff_relative": {"hunk": "--- a/modules/x.py\n+++ b/modules/x.py\n@@ -1 +1 @@\n-a\n+b"},
    "numbers_and_lists": {"n": 3, "f": 0.5, "ok": True, "none": None, "l": [1, "two"]},
    "unicode": {"prompt": "Grüße"},
}


@pytest.mark.parametrize("case", sorted(ACCEPTED))
def test_check_payload_accepts(case):
    check_payload(ACCEPTED[case])


def test_size_limit_boundary():
    # The limit applies to the JSON body; build one just under it.
    overhead = len('{\n  "p": ""\n}')
    check_payload({"p": "x" * (64 * 1024 - overhead)})
    with pytest.raises(PayloadRejected):
        check_payload({"p": "x" * (64 * 1024 - overhead + 1)})


def test_secret_check_reuses_mailbox_scanner(monkeypatch):
    seen: list[str] = []

    def fake_has_secret(text):
        seen.append(text)
        return "MARKER" in text

    monkeypatch.setattr(mailbox_core, "_has_secret", fake_has_secret)
    with pytest.raises(PayloadRejected):
        check_payload({"prompt": "MARKER"})
    assert seen


def test_home_gpu_submit_error_is_a_job_error():
    assert issubclass(HomeGpuSubmitError, HomeGpuJobError)
    from modules.home_gpu import HomeGpuSubmitError as exported
    assert exported is HomeGpuSubmitError
