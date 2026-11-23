"""Issue #759: home-GPU submit_job with the public-data payload guard."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

import pytest

from modules.home_gpu import jobs
from modules.home_gpu.jobs import (
    HomeGpuDisabled,
    HomeGpuJobError,
    PayloadRejected,
    check_payload,
    home_gpu_enabled,
    submit_job,
)
from modules.mailbox import core as mailbox_core
from modules.mailbox import routing


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
def mailbox(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.run_store, "DEFAULT_DB_PATH", str(tmp_path / "agent.db"))
    (tmp_path / "inbox").mkdir()
    return tmp_path


def _inbox_files(mailbox):
    return sorted((mailbox / "inbox").iterdir())


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


def test_disabled_writes_nothing(monkeypatch, tmp_path, git_calls):
    monkeypatch.delenv("NEXUS_HOME_GPU", raising=False)
    with pytest.raises(HomeGpuDisabled):
        submit_job("chat_completion", {"prompt": "hi"}, mailbox_dir=tmp_path, commit=True)
    assert list(tmp_path.iterdir()) == []  # not even the inbox directory
    assert git_calls == []


# --- happy path ---------------------------------------------------------------

def test_front_matter_routing_and_hash(enabled, mailbox, git_calls):
    payload = {"prompt": "Summarise modules/mailbox/routing.py", "max_tokens": 200}
    before = datetime.now(timezone.utc).replace(microsecond=0)
    job = submit_job("chat_completion", payload, mailbox_dir=mailbox, commit=True)
    after = datetime.now(timezone.utc)

    assert _inbox_files(mailbox) == [mailbox / "inbox" / f"{job.job_id}.md"]
    text = job.path.read_text(encoding="utf-8")

    header = text.split("\n---\n", 1)[0]
    lines = header.splitlines()
    assert lines[0] == "---"
    fields = [line.split(":", 1)[0] for line in lines[1:]]
    assert fields == ["type", "to", "created_by", "id", "job_id", "kind", "deadline", "input_sha256"]

    meta, body = mailbox_core.parse_front_matter(text)
    assert meta == {
        "type": "batch_job",
        "to": "home-gpu",
        "created_by": "nexus",
        "id": job.job_id,
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


def test_body_is_deterministic(enabled, mailbox, git_calls):
    a = submit_job("code_draft", {"b": 1, "a": [1, 2]}, mailbox_dir=mailbox, commit=False)
    b = submit_job("code_draft", {"a": [1, 2], "b": 1}, mailbox_dir=mailbox, commit=False)
    assert a.job_id != b.job_id
    assert a.input_sha256 == b.input_sha256


def test_custom_deadline(enabled, mailbox, git_calls):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    job = submit_job("review_draft", {"x": 1}, 60, mailbox_dir=mailbox, commit=False)
    deadline = datetime.strptime(job.deadline, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert before + timedelta(seconds=60) <= deadline <= before + timedelta(seconds=62)


def test_commit_false_skips_git(enabled, mailbox, git_calls):
    job = submit_job("hunk_replay", {"hunk": "@@ -1 +1 @@"}, mailbox_dir=mailbox, commit=False)
    assert job.path.exists()
    assert git_calls == []
    assert not job.committed and not job.pushed


def test_push_failure_is_reported(enabled, mailbox, git_calls, monkeypatch):
    monkeypatch.setattr(mailbox_core, "git_push", lambda repo: False)
    job = submit_job("bakeoff_prompt", {"p": "x"}, mailbox_dir=mailbox, commit=True)
    assert job.committed and not job.pushed


def test_commit_failure_raises(enabled, mailbox, monkeypatch):
    monkeypatch.setattr(mailbox_core, "git_add_and_commit", lambda *a, **k: False)
    monkeypatch.setattr(mailbox_core, "git_push", _no_real_git)
    with pytest.raises(HomeGpuJobError):
        submit_job("bakeoff_prompt", {"p": "x"}, mailbox_dir=mailbox, commit=True)


@pytest.mark.parametrize("kind", sorted(jobs.ALLOWED_KINDS))
def test_every_allowed_kind(enabled, mailbox, git_calls, kind):
    job = submit_job(kind, {"p": "x"}, mailbox_dir=mailbox, commit=False)
    assert f"kind: {kind}\n" in job.path.read_text(encoding="utf-8")


def test_allowed_kinds_exact():
    assert jobs.ALLOWED_KINDS == {
        "chat_completion", "hunk_replay", "bakeoff_prompt", "review_draft", "code_draft",
    }


@pytest.mark.parametrize("kind", ["shell", "", "Chat_Completion", "batch_job"])
def test_unknown_kind_raises_and_writes_nothing(enabled, mailbox, git_calls, kind):
    with pytest.raises(HomeGpuJobError):
        submit_job(kind, {"p": "x"}, mailbox_dir=mailbox, commit=True)
    assert _inbox_files(mailbox) == []
    assert git_calls == []


@pytest.mark.parametrize("deadline_s", [0, -5, 1.5, True])
def test_bad_deadline_raises(enabled, mailbox, git_calls, deadline_s):
    with pytest.raises(HomeGpuJobError):
        submit_job("code_draft", {"p": "x"}, deadline_s, mailbox_dir=mailbox, commit=True)
    assert _inbox_files(mailbox) == []


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
def test_rejected_payload_writes_nothing(enabled, mailbox, git_calls, case):
    with pytest.raises(PayloadRejected):
        submit_job("chat_completion", REJECTED[case], mailbox_dir=mailbox, commit=True)
    assert _inbox_files(mailbox) == []
    assert git_calls == []


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
