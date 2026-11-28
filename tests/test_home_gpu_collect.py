"""Issue #813 part 2b: collect_replies matches reply_to outbox files to queued jobs."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
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
    _reconcile_orphan,
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


def _queue_job(
    db: sqlite3.Connection,
    job_id: str = JOB_ID,
    *,
    deadline: str | None = None,
    submitted_at: str = "2026-01-01T00:00:00Z",
) -> None:
    """Queue a row whose deadline is in the future by default: a reply for a
    row past its deadline is expired at claim time, not collected (#761)."""
    if deadline is None:
        deadline = (
            datetime.now(timezone.utc) + timedelta(hours=6)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    _ensure_schema(db)
    db.execute(
        """
        INSERT INTO home_gpu_jobs
            (job_id, kind, status, submitted_at, deadline, input_sha256)
        VALUES (?, 'chat_completion', 'queued', ?, ?, '0' * 64)
        """,
        (job_id, submitted_at, deadline),
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


# --- failure paths and transaction boundaries (review fixes for #813) --------


def test_db_failure_after_answer_write_removes_orphan_and_retry_succeeds(
    mailbox, db, state_dir, monkeypatch
):
    import modules.home_gpu.collect as collect_module

    _queue_job(db)
    answer = "The home GPU says hi."
    (mailbox / "outbox" / "reply-1.md").write_text(_reply(JOB_ID, answer), encoding="utf-8")
    answer_path = state_dir / "home_gpu" / f"{JOB_ID}.txt"

    real_save_seen = collect_module._save_seen

    def boom(*args, **kwargs):
        # The row UPDATE succeeded and the answer file is already on disk;
        # only the commit-time write fails.
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(collect_module, "_save_seen", boom)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        collect_replies(mailbox, db, state_dir)

    # The transaction rolled back and the orphaned answer file was removed:
    # no file is left on disk that no row represents.
    row = _job_row(db)
    assert row[0] == "queued"
    assert row[7] is None
    assert not answer_path.exists()
    assert _seen(db) == []

    monkeypatch.setattr(collect_module, "_save_seen", real_save_seen)
    assert collect_replies(mailbox, db, state_dir) == 1
    assert _job_row(db)[0] == "answered"
    assert answer_path.read_text(encoding="utf-8") == answer


def test_crash_between_write_and_commit_is_safely_replaced_on_retry(
    mailbox, db, state_dir
):
    _queue_job(db)
    answer = "Fresh answer after the crash."
    (mailbox / "outbox" / "reply-1.md").write_text(_reply(JOB_ID, answer), encoding="utf-8")
    # Simulate a crash after the answer file landed but before the row update
    # committed: an orphan sits at the target path while the row is queued.
    orphan = state_dir / "home_gpu" / f"{JOB_ID}.txt"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("stale bytes from the interrupted attempt", encoding="utf-8")

    assert collect_replies(mailbox, db, state_dir) == 1
    assert _job_row(db)[0] == "answered"
    # The orphan was safely replaced by the atomic tmp-plus-rename write.
    assert orphan.read_text(encoding="utf-8") == answer
    assert not list(orphan.parent.glob("*.tmp-*"))


def test_answer_write_leaves_no_tmp_files(mailbox, db, state_dir):
    _queue_job(db)
    (mailbox / "outbox" / "reply-1.md").write_text(_reply(JOB_ID), encoding="utf-8")

    assert collect_replies(mailbox, db, state_dir) == 1
    assert sorted(p.name for p in (state_dir / "home_gpu").iterdir()) == [
        f"{JOB_ID}.txt"
    ]


def test_reconcile_orphan_removes_only_unrepresented_own_file(db, state_dir):
    _queue_job(db)
    answer_path = state_dir / "home_gpu" / f"{JOB_ID}.txt"
    answer_path.parent.mkdir(parents=True)
    digest = hashlib.sha256(b"mine").hexdigest()

    # Row still queued and the file is ours -> orphan, removed.
    answer_path.write_bytes(b"mine")
    _reconcile_orphan(db, answer_path, digest, JOB_ID)
    assert not answer_path.exists()

    # File content is not ours (a concurrent winner wrote something else) -> kept.
    answer_path.write_bytes(b"theirs")
    _reconcile_orphan(db, answer_path, digest, JOB_ID)
    assert answer_path.read_bytes() == b"theirs"

    # Row answered with this exact path -> the file is represented, kept.
    db.execute(
        "UPDATE home_gpu_jobs SET status = 'answered', answer_path = ? WHERE job_id = ?",
        (str(answer_path), JOB_ID),
    )
    db.commit()
    _reconcile_orphan(db, answer_path, digest, JOB_ID)
    assert answer_path.read_bytes() == b"theirs"

    # A missing file is a no-op.
    answer_path.unlink()
    _reconcile_orphan(db, answer_path, digest, JOB_ID)
    assert not answer_path.exists()


def test_row_answered_between_check_and_update_is_ignored_once(
    mailbox, tmp_path, state_dir, monkeypatch, caplog
):
    import modules.mailbox.core as mailbox_core

    db_path = tmp_path / "race.db"
    db = init_db(str(db_path))
    try:
        _queue_job(db)
        (mailbox / "outbox" / "reply-1.md").write_text(_reply(JOB_ID), encoding="utf-8")
        answer_path = state_dir / "home_gpu" / f"{JOB_ID}.txt"
        real_write = mailbox_core._atomic_write

        def write_then_lose_the_race(path, content):
            real_write(path, content)
            # A concurrent collector answers the row after our status check
            # but before our guarded UPDATE claims it.
            other = sqlite3.connect(str(db_path))
            other.execute(
                "UPDATE home_gpu_jobs SET status = 'answered', answer_sha256 = ?, "
                "answer_path = ? WHERE job_id = ? AND status = 'queued'",
                ("f" * 64, str(answer_path), JOB_ID),
            )
            other.commit()
            other.close()

        monkeypatch.setattr(mailbox_core, "_atomic_write", write_then_lose_the_race)
        caplog.set_level(logging.WARNING)

        assert collect_replies(mailbox, db, state_dir) == 0
        assert caplog.text.count("job already answered") == 1
        row = _job_row(db)
        assert row[0] == "answered"
        assert row[6] == "f" * 64  # the winner's values, not ours
        assert answer_path.exists()  # the winner's file is kept, not deleted
        assert _seen(db) == ["reply-1.md"]

        caplog.clear()
        assert collect_replies(mailbox, db, state_dir) == 0
        assert "job already answered" not in caplog.text
    finally:
        db.close()


def test_collect_replies_preserves_caller_open_transaction(tmp_path, mailbox, state_dir):
    db_path = tmp_path / "tx.db"
    db = init_db(str(db_path))
    try:
        _queue_job(db)  # commits schema + job row
        (mailbox / "outbox" / "reply-1.md").write_text(_reply(JOB_ID), encoding="utf-8")
        # The caller has an open, uncommitted transaction on the same connection.
        db.execute(
            "INSERT INTO service_settings (key, value, updated_at) VALUES "
            "('caller.marker', 'x', '2026-01-01T00:00:00Z')"
        )
        assert db.in_transaction

        other = sqlite3.connect(str(db_path))
        try:
            assert collect_replies(mailbox, db, state_dir) == 1
            # Our work is visible on this connection ...
            assert _job_row(db)[0] == "answered"
            assert _seen(db) == ["reply-1.md"]
            # ... but nothing was committed: the caller's transaction is still
            # open and a second connection sees neither our work nor the
            # caller's pending insert.
            assert db.in_transaction
            assert (
                other.execute(
                    "SELECT status FROM home_gpu_jobs WHERE job_id = ?", (JOB_ID,)
                ).fetchone()[0]
                == "queued"
            )
            assert (
                other.execute(
                    "SELECT COUNT(*) FROM service_settings WHERE key = 'caller.marker'"
                ).fetchone()[0]
                == 0
            )
            assert (
                other.execute(
                    "SELECT COUNT(*) FROM service_settings WHERE key = ?",
                    (SEEN_FILES_SETTING,),
                ).fetchone()[0]
                == 0
            )
        finally:
            other.close()
        db.rollback()
        # Our update rolled back together with the caller's transaction.
        assert _job_row(db)[0] == "queued"
    finally:
        db.close()


def test_ensure_schema_never_commits_a_pending_transaction(tmp_path):
    db_path = tmp_path / "schema.db"
    db = init_db(str(db_path))  # has service_settings, not home_gpu_jobs
    try:
        db.execute(
            "INSERT INTO service_settings (key, value, updated_at) VALUES "
            "('caller.marker', 'x', '2026-01-01T00:00:00Z')"
        )
        assert db.in_transaction
        _ensure_schema(db)
        # The schema was created (visible on this connection) ...
        assert (
            db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'home_gpu_jobs'"
            ).fetchone()[0]
            == 1
        )
        # ... but the caller's transaction was NOT committed.
        assert db.in_transaction
        other = sqlite3.connect(str(db_path))
        try:
            assert (
                other.execute(
                    "SELECT COUNT(*) FROM service_settings WHERE key = 'caller.marker'"
                ).fetchone()[0]
                == 0
            )
            assert (
                other.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name = 'home_gpu_jobs'"
                ).fetchone()[0]
                == 0
            )
        finally:
            other.close()
        db.rollback()

        # With no transaction open the DDL autocommits and persists on its own,
        # and a repeated call is a read-only probe that starts no transaction.
        assert not db.in_transaction
        _ensure_schema(db)
        _ensure_schema(db)
        assert not db.in_transaction
        assert (
            db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'home_gpu_jobs'"
            ).fetchone()[0]
            == 1
        )
        other2 = sqlite3.connect(str(db_path))
        try:
            assert (
                other2.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name = 'home_gpu_jobs'"
                ).fetchone()[0]
                == 1
            )
        finally:
            other2.close()
    finally:
        db.close()


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


# --- deadline enforcement at claim time and during the poll (#761) ---------------


def test_reply_for_a_job_past_its_deadline_is_expired_not_claimed(mailbox, db, state_dir, caplog):
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _queue_job(db, deadline=past)
    (mailbox / "outbox" / "reply-1.md").write_text(_reply(JOB_ID), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=COLLECT_LOGGER):
        assert collect_replies(mailbox, db, state_dir) == 0

    # The row became expired at claim time; no answer file, reply marked seen.
    assert _job_row(db)[0] == "expired"
    assert not (state_dir / "home_gpu").exists()
    assert "reply-1.md" in _seen(db)
    assert any("deadline passed" in rec.message for rec in caplog.records)
    # A repeated run over the same outbox is idempotent and silent on this file.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=COLLECT_LOGGER):
        assert collect_replies(mailbox, db, state_dir) == 0
    assert not any("deadline passed" in rec.message for rec in caplog.records)


def test_reply_for_a_job_inside_its_deadline_is_claimed(mailbox, db, state_dir):
    future = (datetime.now(timezone.utc) + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _queue_job(db, deadline=future)
    (mailbox / "outbox" / "reply-1.md").write_text(_reply(JOB_ID), encoding="utf-8")

    assert collect_replies(mailbox, db, state_dir) == 1
    assert _job_row(db)[0] == "answered"


def test_tick_with_flag_on_expires_past_deadline_jobs_before_collecting(tmp_path, monkeypatch):
    """Expiry runs during normal mailbox polling, not only on submission: a
    queued row past its deadline is expired by the tick and its late reply is
    never claimed."""
    monkeypatch.setenv("NEXUS_HOME_GPU", "1")
    code_repo, mailbox_repo, db_path = _tick_setup(tmp_path)
    db = init_db(str(db_path))
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _queue_job(db, deadline=past)
    (mailbox_repo / "outbox" / "reply-1.md").write_text(_reply(JOB_ID), encoding="utf-8")
    _patch_tick(monkeypatch, _FakeProcessor(db))

    cfg = MailboxConfig.from_paths(code_repo, mailbox_repo)
    result = run_mailbox_poll_once(
        db_path=str(db_path),
        mailbox_config=cfg,
        lock_path=tmp_path / "mailbox.lock",
        dry_run=False,
    )
    assert result.status == "processed"
    assert _job_row(db)[0] == "expired"
    assert not (code_repo / "state" / "home_gpu").exists()
    db.close()


def test_tick_with_flag_on_collects_a_reply_inside_its_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME_GPU", "1")
    code_repo, mailbox_repo, db_path = _tick_setup(tmp_path)
    db = init_db(str(db_path))
    _queue_job(db)  # future deadline by default
    (mailbox_repo / "outbox" / "reply-1.md").write_text(_reply(JOB_ID), encoding="utf-8")
    _patch_tick(monkeypatch, _FakeProcessor(db))

    cfg = MailboxConfig.from_paths(code_repo, mailbox_repo)
    result = run_mailbox_poll_once(
        db_path=str(db_path),
        mailbox_config=cfg,
        lock_path=tmp_path / "mailbox.lock",
        dry_run=False,
    )
    assert result.status == "processed"
    assert _job_row(db)[0] == "answered"
    db.close()
