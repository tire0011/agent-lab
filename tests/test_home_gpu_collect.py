"""Issue #760 reply collection through the mailbox and read interfaces."""

import asyncio
import hashlib
from contextlib import closing
from pathlib import Path

import pytest

from modules.home_gpu.collect import MAX_ANSWER_BYTES, collect_replies
from modules.home_gpu.jobs import submit_job
from modules.run_store import init_db


@pytest.fixture
def queued(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME_GPU", "1")
    db_path = tmp_path / "state" / "agent.db"
    mailbox = tmp_path / "mailbox"
    (mailbox / "outbox").mkdir(parents=True)
    job = submit_job("chat_completion", {"prompt": "Hello"}, mailbox_dir=mailbox,
                     commit=False, db_path=db_path)
    return mailbox, db_path, job


def _reply(job_id, answer="  First line\nLast line.\n"):
    # mailbox_client.outbox.build_reply fields, followed by the GPU's extra
    # metadata and the client's blank lines, result heading and final newline.
    return (
        f"---\nsource: inbox/{job_id.removesuffix('.md')}.md\ntype: batch_job\n"
        "status: done\nprocessed_at: 2026-11-20T12:00:00Z\ncreated_by: nexus\n"
        f"to: nexus\nreply_to: {job_id}\nid: 704e2196c8af4f56ad28507744223404\n"
        "agent: home-gpu\nmodel: local-model\nprompt_tokens: 17\n"
        "completion_tokens: 5\nseconds: 1.25\nhost: workstation\n"
        f"---\n\n## Result\n\n{answer}\n"
    )


def test_collects_answer_and_leaves_job_unchanged_on_duplicate(queued, caplog):
    mailbox, db_path, job = queued
    answer = "  First line\nLast line.\n"
    reply = _reply(job.job_id, answer)
    with closing(init_db(db_path)) as db:
        assert db.execute(
            "SELECT status, deadline, input_sha256 FROM home_gpu_jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone() == ("queued", job.deadline, job.input_sha256)
        (mailbox / "outbox" / "reply.md").write_text(reply, encoding="utf-8")
        assert collect_replies(mailbox, db) == 1
        row = db.execute(
            """SELECT status, model, prompt_tokens, completion_tokens, seconds, host,
                      answered_at, answer_sha256, answer_path FROM home_gpu_jobs"""
        ).fetchone()
        assert row[:6] == ("answered", "local-model", 17, 5, 1.25, "workstation")
        assert row[6]
        assert row[7] == hashlib.sha256(answer.encode()).hexdigest()
        path = Path(row[8])
        assert path.is_relative_to(db_path.parent)
        assert path.read_bytes() == answer.encode()
        assert collect_replies(mailbox, db) == 0
        assert "job is no longer queued" not in caplog.text
        (mailbox / "outbox" / "copy.md").write_text(reply, encoding="utf-8")
        assert collect_replies(mailbox, db) == 0
        assert db.execute("SELECT answer_path, answered_at FROM home_gpu_jobs").fetchone() == (str(path), row[6])
    with closing(init_db(db_path)) as db:
        assert collect_replies(mailbox, db) == 0
    assert caplog.text.count("job is no longer queued") == 1


def test_ignores_unknown_oversized_malformed_and_legacy_replies(queued, caplog):
    mailbox, db_path, job = queued
    outbox = mailbox / "outbox"
    (outbox / "unknown.md").write_text(_reply("unknown"), encoding="utf-8")
    (outbox / "big.md").write_text(_reply(job.job_id, "é" * (MAX_ANSWER_BYTES // 2 + 1)), encoding="utf-8")
    (outbox / "malformed.md").write_text(
        _reply(job.job_id).replace("seconds: 1.25", "seconds: nan"), encoding="utf-8"
    )
    (outbox / "legacy.md").write_text(
        _reply(job.job_id).replace("reply_to:", "in_reply_to:"), encoding="utf-8"
    )
    for _ in range(2):
        with closing(init_db(db_path)) as db:
            assert collect_replies(mailbox, db) == 0
            assert db.execute("SELECT status FROM home_gpu_jobs").fetchall() == [("queued",)]
    for reason in ("unknown job", "oversized answer", "malformed reply"):
        assert caplog.text.count(reason) == 1
    assert not (db_path.parent / "home_gpu_answers").exists()


def test_accepts_mailbox_client_filename_correlation(queued):
    mailbox, db_path, job = queued
    (mailbox / "outbox" / "reply.md").write_text(_reply(f"{job.job_id}.md"), encoding="utf-8")
    with closing(init_db(db_path)) as db:
        assert collect_replies(mailbox, db) == 1


def test_failed_answer_write_leaves_job_queued_for_retry(queued, monkeypatch):
    from modules.home_gpu import collect

    mailbox, db_path, job = queued
    (mailbox / "outbox" / "reply.md").write_text(_reply(job.job_id), encoding="utf-8")

    def fail_write(path, answer):
        raise OSError("answer write failed")

    with closing(init_db(db_path)) as db:
        with monkeypatch.context() as patch:
            patch.setattr(collect, "_atomic_write", fail_write)
            with pytest.raises(OSError, match="answer write failed"):
                collect_replies(mailbox, db)
        assert db.execute("SELECT status, answer_path FROM home_gpu_jobs").fetchone() == ("queued", None)
        assert collect_replies(mailbox, db) == 1


def test_authenticated_route_and_mcp_tool_list_answered_job(queued, monkeypatch):
    asyncio.run(_check_read_interfaces(queued, monkeypatch))


async def _check_read_interfaces(queued, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    monkeypatch.setenv("AGENT_API_KEY", "home-gpu-test-key")
    import api
    from nexus_mcp import create_mcp_mount

    mailbox, db_path, job = queued
    (mailbox / "outbox" / "reply.md").write_text(_reply(job.job_id), encoding="utf-8")
    with closing(init_db(db_path)) as db:
        assert collect_replies(mailbox, db) == 1

    monkeypatch.setattr(api, "DEFAULT_DB_PATH", str(db_path))
    monkeypatch.setattr(api, "API_KEY", "home-gpu-test-key")

    auth = {"x-api-key": "home-gpu-test-key"}
    async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://testserver") as client:
        assert (await client.get("/home-gpu/jobs")).status_code == 401
        assert (await client.get("/home-gpu/jobs?limit=0", headers=auth)).status_code == 422
        response = await client.get("/home-gpu/jobs?limit=1", headers=auth)
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["jobs"][0]["job_id"] == job.job_id
    assert payload["jobs"][0]["status"] == "answered"

    mount = create_mcp_mount()
    mcp_app = FastAPI()
    mcp_app.mount("/mcp", mount)
    async with mount.lifespan(), AsyncClient(
        transport=ASGITransport(app=mcp_app), base_url="http://testserver"
    ) as mcp_client:
        response = await mcp_client.post(
            "/mcp/",
            headers={**auth, "accept": "application/json, text/event-stream",
                     "mcp-protocol-version": "2025-06-18"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "home_gpu_jobs_list", "arguments": {"limit": 1}}},
        )
    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"] == payload


@pytest.mark.parametrize("enabled", [False, True])
def test_mailbox_tick_collects_only_when_enabled(queued, monkeypatch, enabled):
    from unittest.mock import MagicMock

    import modules.mailbox as mailbox_module
    from modules import outbox_pr_notifier, outbox_publisher
    from modules.mailbox import poll_runner
    from modules.mailbox.config import MailboxConfig

    mailbox, db_path, job = queued
    outbox = mailbox / "outbox"
    (outbox / "reply.md").write_text(_reply(job.job_id), encoding="utf-8")
    monkeypatch.setenv("NEXUS_HOME_GPU", "1" if enabled else "0")
    monkeypatch.setattr(poll_runner, "_worktree_safe_for_mailbox_sync", lambda *a: True)
    monkeypatch.setattr(poll_runner, "_sync_mailbox_repo", lambda *a, **k: True)
    monkeypatch.setattr(poll_runner, "_drain_inbox", lambda **kw: poll_runner._DrainOutcome([]))
    monkeypatch.setattr(mailbox_module.MailboxProcessor, "retry_pending_git_commits", lambda self: None)
    monkeypatch.setattr(mailbox_module.MailboxProcessor, "prune_old_messages", lambda self: None)
    monkeypatch.setattr(outbox_pr_notifier, "notifier_from_env", lambda **kw: None)
    for name in ("TaskApprovalOutboxPublisher", "FailureAnalysisOutboxPublisher"):
        monkeypatch.setattr(outbox_publisher, name, MagicMock())
    scans = []
    glob = Path.glob

    def track_glob(path, pattern):
        if path == outbox:
            scans.append(pattern)
        return glob(path, pattern)

    monkeypatch.setattr(Path, "glob", track_glob)
    result = poll_runner.run_mailbox_poll_once(
        db_path=str(db_path),
        mailbox_config=MailboxConfig.from_paths(mailbox.parent, mailbox),
        lock_path=mailbox.parent / "mailbox.lock",
        dry_run=False,
    )
    assert result.status == "processed", result.error
    assert scans == (["*.md"] if enabled else [])
    with closing(init_db(db_path)) as db:
        status = db.execute("SELECT status FROM home_gpu_jobs").fetchone()[0]
    assert status == ("answered" if enabled else "queued")
