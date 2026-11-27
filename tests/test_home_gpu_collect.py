"""Issue #813 part 2b: collect_replies matches reply_to outbox files to queued jobs."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path

import pytest

import modules.experiment_service as experiment_service_module
import modules.mailbox as mailbox_module
import modules.mailbox.poll_runner as poll_runner_module
import modules.operation_chain_publisher as operation_chain_publisher_module
import modules.operation_chain_service as operation_chain_service_module
import modules.operations_registry as operations_registry_module
import modules.outbox_pr_notifier as outbox_pr_notifier_module
import modules.outbox_publisher as outbox_publisher_module
from modules.home_gpu.collect import (
    MAX_ANSWER_BYTES,
    SEEN_FILES_SETTING,
    _ensure_schema,
    collect_replies,
)
from modules.mailbox.config import MailboxConfig
from modules.mailbox.poll_runner import run_mailbox_poll_once
from modules.run_store import init_db

JOB_ID = "hg-20260101T000000Z-0123456789ab"
COLLECT_LOGGER = "modules.home_gpu.collect"


def _reply(job_id: str, answer: str = "The home GPU says hi.") -> str:
    """A mailbox_client reply in the exact format: front matter with reply_to,
    status, model, prompt_tokens, completion_tokens, seconds; answer in the body."""
    return (
        "---\n"
        f"reply_to: {job_id}\n"
        "status: done\n"
        "model: local-model\n"
        "prompt_tokens: 17\n"
        "completion_tokens: 5\n"
        "seconds: 1.25\n"
        "---\n"
        f"{answer}\n"
    )


def _queue_job(db: sqlite3.Connection, job_id: str = JOB_ID) -> None:
    _ensure_schema(db)
    db.execute(
        """
        INSERT INTO home_gpu_jobs
            (job_id, kind, status, submitted_at, deadline, input_sha256)
        VALUES (?, 'chat_completion', 'queued', '2026-01-01T00:00:00Z',
                '2026-01-01T06:00:00Z', '0' * 64)
        """,
        (job_id,),
    )
    db.commit()


def _job_row(db: sqlite3.Connection, job_id: str = JOB_ID):
    columns = (
        "status, answered_at, model, prompt_tokens, completion_tokens, "
        "seconds, answer_sha256, answer_path"
    )
    return db.execute(
        f"SELECT {columns} FROM home_gpu_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()


def _seen(db: sqlite3.Connection) -> list[str]:
    row = db.execute(
        "SELECT value FROM service_settings WHERE key = ?", (SEEN_FILES_SETTING,)
    ).fetchone()
    if row is None:
        return []
    return json.loads(row[0])


@pytest.fixture
def mailbox(tmp_path):
    repo = tmp_path / "mailbox"
    (repo / "inbox").mkdir(parents=True)
    (repo / "outbox").mkdir()
    return repo


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def db(tmp_path):
    conn = init_db(str(tmp_path / "agent.db"))
    yield conn
    conn.close()


# --- collect_replies ---------------------------------------------------------


def test_fixture_reply_makes_row_answered_with_its_numbers(mailbox, db, state_dir):
    _queue_job(db)
    answer = "The home GPU says hi."
    (mailbox / "outbox" / "reply-1.md").write_text(_reply(JOB_ID, answer), encoding="utf-8")

    assert collect_replies(mailbox, db, state_dir) == 1

    row = _job_row(db)
    assert row[0] == "answered"
    assert row[1]  # answered_at
    assert row[2] == "local-model"
    assert row[3] == 17
    assert row[4] == 5
    assert row[5] == 1.25
    assert row[6] == hashlib.sha256(answer.encode("utf-8")).hexdigest()
    expected_path = state_dir / "home_gpu" / f"{JOB_ID}.txt"
    assert row[7] == str(expected_path)
    assert expected_path.read_text(encoding="utf-8") == answer
    assert _seen(db) == ["reply-1.md"]


def test_second_copy_changes_nothing_and_logs_nothing_new(mailbox, db, state_dir, caplog):
    _queue_job(db)
    (mailbox / "outbox" / "reply-1.md").write_text(_reply(JOB_ID), encoding="utf-8")
    caplog.set_level(logging.WARNING)

    assert collect_replies(mailbox, db, state_dir) == 1
    before = _job_row(db)

    caplog.clear()
    assert collect_replies(mailbox, db, state_dir) == 0
    assert _job_row(db) == before
    assert [r for r in caplog.records if r.name == COLLECT_LOGGER] == []


def test_repeated_reply_for_answered_row_is_ignored_and_logged_once_per_file(
    mailbox, db, state_dir, caplog
):
    _queue_job(db)
    (mailbox / "outbox" / "reply-1.md").write_text(_reply(JOB_ID), encoding="utf-8")
    assert collect_replies(mailbox, db, state_dir) == 1
    before = _job_row(db)

    caplog.set_level(logging.WARNING)
    (mailbox / "outbox" / "reply-2.md").write_text(_reply(JOB_ID), encoding="utf-8")
    assert collect_replies(mailbox, db, state_dir) == 0
    assert caplog.text.count("job already answered") == 1
    assert _job_row(db) == before

    caplog.clear()
    assert collect_replies(mailbox, db, state_dir) == 0
    assert "job already answered" not in caplog.text
    assert _seen(db) == ["reply-1.md", "reply-2.md"]


def test_unknown_id_is_ignored_and_logged_once(mailbox, db, state_dir, caplog):
    _queue_job(db)
    (mailbox / "outbox" / "unknown.md").write_text(
        _reply("hg-20260101T000000Z-ffffffffffff"), encoding="utf-8"
    )
    (mailbox / "outbox" / "escape.md").write_text(
        _reply("../escape"), encoding="utf-8"
    )
    caplog.set_level(logging.WARNING)

    assert collect_replies(mailbox, db, state_dir) == 0
    assert _job_row(db)[0] == "queued"
    assert not (state_dir / "home_gpu").exists()
    messages = [r.getMessage() for r in caplog.records if r.name == COLLECT_LOGGER]
    assert sum("reason=unknown job job_id" in m for m in messages) == 1
    assert sum("reason=unknown job id" in m for m in messages) == 1

    caplog.clear()
    assert collect_replies(mailbox, db, state_dir) == 0
    assert [r for r in caplog.records if r.name == COLLECT_LOGGER] == []


def test_oversized_body_is_ignored_and_logged_once(mailbox, db, state_dir, caplog):
    _queue_job(db)
    big = "x" * (300 * 1024)  # over the 256 KB cap
    assert len(big.encode("utf-8")) > MAX_ANSWER_BYTES
    (mailbox / "outbox" / "big.md").write_text(_reply(JOB_ID, big), encoding="utf-8")
    caplog.set_level(logging.WARNING)

    assert collect_replies(mailbox, db, state_dir) == 0
    assert _job_row(db)[0] == "queued"
    assert not (state_dir / "home_gpu").exists()
    assert caplog.text.count("over 256 KB") == 1

    caplog.clear()
    assert collect_replies(mailbox, db, state_dir) == 0
    assert "over 256 KB" not in caplog.text


def test_in_reply_to_is_not_used(mailbox, db, state_dir):
    _queue_job(db)
    text = _reply(JOB_ID).replace("reply_to:", "in_reply_to:")
    (mailbox / "outbox" / "legacy.md").write_text(text, encoding="utf-8")

    assert collect_replies(mailbox, db, state_dir) == 0
    assert _job_row(db)[0] == "queued"
    assert not (state_dir / "home_gpu").exists()


def test_missing_outbox_is_a_no_op(tmp_path, db, state_dir):
    assert collect_replies(tmp_path / "nowhere", db, state_dir) == 0


def test_failed_answer_write_leaves_row_queued_for_retry(mailbox, db, state_dir, monkeypatch):
    import modules.mailbox.core as mailbox_core

    _queue_job(db)
    (mailbox / "outbox" / "reply-1.md").write_text(_reply(JOB_ID), encoding="utf-8")

    def boom(_path, _content):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(mailbox_core, "_atomic_write", boom)
        with pytest.raises(OSError, match="disk full"):
            collect_replies(mailbox, db, state_dir)
    # The transaction rolled back: still queued, no answer columns, not seen.
    row = _job_row(db)
    assert row[0] == "queued"
    assert row[7] is None
    assert _seen(db) == []

    assert collect_replies(mailbox, db, state_dir) == 1
    assert _job_row(db)[0] == "answered"


# --- mailbox tick integration -------------------------------------------------


class _FakeLock:
    def __init__(self, _path):
        pass

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        pass


class _FakeService:
    def __init__(self, **_kwargs):
        pass

    def publish_pending(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeProcessor:
    def __init__(self, conn):
        self.conn = conn

    def retry_pending_git_commits(self) -> None:
        pass

    def prune_old_messages(self) -> None:
        pass

    def list_inbox_files(self) -> list[Path]:
        return []

    def process_inbox(self, files=None) -> list[dict]:
        return []

    def close(self) -> None:
        pass


def _patch_tick(monkeypatch, processor) -> None:
    monkeypatch.setattr(mailbox_module, "MailboxLock", _FakeLock)
    monkeypatch.setattr(mailbox_module, "MailboxProcessor", lambda **_kw: processor)
    monkeypatch.setattr(experiment_service_module, "ExperimentService", _FakeService)
    monkeypatch.setattr(operations_registry_module, "OperationsRegistry", _FakeService)
    monkeypatch.setattr(operation_chain_service_module, "OperationChainService", _FakeService)
    monkeypatch.setattr(operation_chain_publisher_module, "OperationChainPublisher", _FakeService)
    monkeypatch.setattr(outbox_publisher_module, "TaskApprovalOutboxPublisher", _FakeService)
    monkeypatch.setattr(outbox_publisher_module, "FailureAnalysisOutboxPublisher", _FakeService)
    monkeypatch.setattr(outbox_pr_notifier_module, "notifier_from_env", lambda **_kw: None)
    monkeypatch.setattr(
        poll_runner_module, "_worktree_safe_for_mailbox_sync", lambda *a, **kw: True
    )
    monkeypatch.setattr(poll_runner_module, "_sync_mailbox_repo", lambda *a, **kw: True)


def _tick_setup(tmp_path):
    code_repo = tmp_path / "code"
    (code_repo / "state").mkdir(parents=True)
    mailbox_repo = tmp_path / "mailbox"
    (mailbox_repo / "inbox").mkdir(parents=True)
    (mailbox_repo / "outbox").mkdir()
    db_path = tmp_path / "agent.db"
    return code_repo, mailbox_repo, db_path


def test_tick_with_flag_off_never_reads_outbox(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_HOME_GPU", raising=False)
    code_repo, mailbox_repo, db_path = _tick_setup(tmp_path)
    db = init_db(str(db_path))
    _queue_job(db)
    (mailbox_repo / "outbox" / "reply-1.md").write_text(_reply(JOB_ID), encoding="utf-8")
    _patch_tick(monkeypatch, _FakeProcessor(db))

    import modules.home_gpu.collect as collect_module

    calls: list[tuple] = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("collect_replies called with the flag off")

    monkeypatch.setattr(collect_module, "collect_replies", spy)

    real_iterdir = Path.iterdir

    def guarded_iterdir(self, *args, **kwargs):
        if self == mailbox_repo / "outbox":
            raise AssertionError("tick read outbox/ with the flag off")
        return real_iterdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

    cfg = MailboxConfig.from_paths(code_repo, mailbox_repo)
    result = run_mailbox_poll_once(
        db_path=str(db_path),
        mailbox_config=cfg,
        lock_path=tmp_path / "mailbox.lock",
        dry_run=False,
    )
    assert result.status == "processed"
    assert calls == []
    assert _job_row(db)[0] == "queued"
    assert not (code_repo / "state" / "home_gpu").exists()
    db.close()


def test_tick_with_flag_on_collects_replies(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME_GPU", "1")
    code_repo, mailbox_repo, db_path = _tick_setup(tmp_path)
    db = init_db(str(db_path))
    _queue_job(db)
    answer = "Collected by the tick."
    (mailbox_repo / "outbox" / "reply-1.md").write_text(
        _reply(JOB_ID, answer), encoding="utf-8"
    )
    _patch_tick(monkeypatch, _FakeProcessor(db))

    cfg = MailboxConfig.from_paths(code_repo, mailbox_repo)
    result = run_mailbox_poll_once(
        db_path=str(db_path),
        mailbox_config=cfg,
        lock_path=tmp_path / "mailbox.lock",
        dry_run=False,
    )
    assert result.status == "processed"
    row = _job_row(db)
    assert row[0] == "answered"
    assert row[2] == "local-model"
    assert row[3] == 17
    assert row[4] == 5
    assert row[5] == 1.25
    expected = code_repo / "state" / "home_gpu" / f"{JOB_ID}.txt"
    assert row[7] == str(expected)
    assert expected.read_text(encoding="utf-8") == answer
    db.close()
