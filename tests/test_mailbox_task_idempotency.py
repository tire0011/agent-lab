"""Mailbox task ingestion is idempotent per persistent key (#378)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modules.mailbox.core import MailboxMessage
from modules.mailbox.processing import _process_task
from modules.mailbox.task_ingest import insert_task_approval
from modules.run_store import init_db
from tests.test_mailbox_task_service import _make_exp, _make_service

def _wait_for_failure_analyses(svc, timeout=None):
    from concurrent.futures import wait as _fut_wait
    with svc._failure_analysis_futures_lock:
        pending = tuple(svc._failure_analysis_futures)
    if not pending:
        return True
    _done, not_done = _fut_wait(pending, timeout=timeout)
    return not not_done

_RECEIVED = "2026-01-01T00:00:00Z"


def _exp_service(tmp_path: Path) -> MagicMock:
    exp = _make_exp(id="exp-idem", status="queued")
    es = MagicMock()
    es.create_experiment.return_value = exp
    es.get_experiment.return_value = exp
    es.run_experiment.return_value = exp
    es.experiment_log_dir = str(tmp_path / "experiment-logs")
    return es


def _insert_msg(db_path: str, file_path: str = "/agent/inbox/t.md") -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO mailbox_messages (file_path, source_commit_sha, msg_type, created_by, status, received_at) "
            "VALUES (?, 'sha', 'task', 'tester', 'done', ?)",
            (file_path, _RECEIVED),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _task_count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM mailbox_task_approvals").fetchone()[0]
    finally:
        conn.close()


def _dispatch_all(svc) -> None:
    """Drain the direct-to-Nexus queue more often than needed."""
    for _ in range(10):
        svc.submit_queued()
    _wait_for_failure_analyses(svc, timeout=5)


@pytest.fixture
def svc(tmp_path):
    service = _make_service(tmp_path, exp_service=_exp_service(tmp_path))
    # _make_service's seed message is backfilled into a task; start empty.
    service._conn.execute("DELETE FROM mailbox_task_approvals")
    service._conn.commit()
    yield service
    service.close()


def test_sequential_redelivery_returns_original_task_and_dispatches_once(svc):
    msg_id = _insert_msg(svc.db_path)

    first = svc.create_task(msg_id, "Do work", "alice", "/f.md", _RECEIVED)
    again = [svc.create_task(msg_id, "Do work", "alice", "/f.md", _RECEIVED) for _ in range(4)]

    assert {a.id for a in again} == {first.id}
    assert first.idempotency_key == f"mailbox-msg:{msg_id}"
    assert _task_count(svc.db_path) == 1

    _dispatch_all(svc)
    # A delivery after dispatch still returns the original, now-submitted task.
    late = svc.create_task(msg_id, "Do work", "alice", "/f.md", _RECEIVED)
    _dispatch_all(svc)

    assert late.id == first.id
    assert late.experiment_id == "exp-idem"
    svc.experiment_service.create_experiment.assert_called_once()
    assert _task_count(svc.db_path) == 1
    assert getattr(svc, "_nexus_task_store", None) is None


def test_explicit_key_deduplicates_across_source_messages(svc):
    msg_a = _insert_msg(svc.db_path, "/agent/inbox/a.md")
    msg_b = _insert_msg(svc.db_path, "/agent/inbox/b.md")

    a = svc.create_task(msg_a, "Do work", "alice", "/a.md", _RECEIVED, idempotency_key="req-1")
    b = svc.create_task(msg_b, "Do work", "alice", "/b.md", _RECEIVED, idempotency_key="req-1")

    assert a.id == b.id
    assert a.idempotency_key == "client:req-1"
    assert _task_count(svc.db_path) == 1


def test_concurrent_deliveries_through_one_service_create_and_dispatch_once(svc):
    msg_id = _insert_msg(svc.db_path)
    workers = 16
    barrier = threading.Barrier(workers)
    results: list[str] = []
    errors: list[BaseException] = []

    def deliver() -> None:
        try:
            barrier.wait()
            results.append(
                svc.create_task(msg_id, "Do work", "alice", "/f.md", _RECEIVED).id
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=deliver) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == workers and len(set(results)) == 1
    assert _task_count(svc.db_path) == 1
    _dispatch_all(svc)
    svc.experiment_service.create_experiment.assert_called_once()


def test_concurrent_deliveries_on_separate_connections_create_one_task(tmp_path):
    """The unique index, not the in-process lock, is what holds the line."""
    db_path = str(tmp_path / "shared.db")
    init_db(db_path).close()
    msg_id = _insert_msg(db_path)
    workers = 12
    barrier = threading.Barrier(workers)
    outcomes: list[tuple[str, bool]] = []
    errors: list[BaseException] = []

    def deliver() -> None:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            barrier.wait()
            outcomes.append(insert_task_approval(
                conn,
                mailbox_msg_id=msg_id,
                task_body="Do work",
                sender="alice",
                file_path="/f.md",
                received_at=_RECEIVED,
                idempotency_key="api-call-7",
            ))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=deliver) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len({approval_id for approval_id, _ in outcomes}) == 1
    assert sum(created for _, created in outcomes) == 1
    assert _task_count(db_path) == 1


def test_distinct_keys_with_identical_text_create_separate_tasks(svc):
    msg_a = _insert_msg(svc.db_path, "/agent/inbox/a.md")
    msg_b = _insert_msg(svc.db_path, "/agent/inbox/b.md")

    a = svc.create_task(msg_a, "Same text", "alice", "/a.md", _RECEIVED)
    b = svc.create_task(msg_b, "Same text", "alice", "/b.md", _RECEIVED)
    c = svc.create_task(msg_b, "Same text", "alice", "/b.md", _RECEIVED, idempotency_key="other")

    assert len({a.id, b.id, c.id}) == 3
    assert _task_count(svc.db_path) == 3
    _dispatch_all(svc)
    assert svc.experiment_service.create_experiment.call_count == 3


@pytest.mark.parametrize("bad_key", ["", "   ", "has space", "x" * 201, "tab\there"])
def test_malformed_explicit_key_is_rejected(svc, bad_key):
    msg_id = _insert_msg(svc.db_path)
    with pytest.raises(ValueError):
        svc.create_task(msg_id, "Do work", "alice", "/f.md", _RECEIVED, idempotency_key=bad_key)
    assert _task_count(svc.db_path) == 0


def _inbox_message(tmp_path: Path, name: str, front: str) -> MailboxMessage:
    path = tmp_path / name
    path.write_text(f"---\ntype: task\ncreated_by: alice\n{front}---\nDo work\n", encoding="utf-8")
    return MailboxMessage.load(path, tmp_path)


def test_inbox_front_matter_key_deduplicates_redelivered_files(tmp_path):
    db_path = str(tmp_path / "inbox.db")
    conn = init_db(db_path)
    first = _inbox_message(tmp_path, "a.md", "idempotency_key: deliver-42\n")
    second = _inbox_message(tmp_path, "b.md", "idempotency_key: deliver-42\n")
    plain = _inbox_message(tmp_path, "c.md", "")

    for msg in (first, second, plain):
        _process_task(msg, tmp_path, conn, _insert_msg(db_path, str(msg.file_path)))

    keys = sorted(row[0] for row in conn.execute(
        "SELECT idempotency_key FROM mailbox_task_approvals"
    ))
    conn.close()
    assert len(keys) == 2
    assert keys[0] == "client:deliver-42"
    assert keys[1].startswith("mailbox-msg:")


def test_inbox_malformed_front_matter_key_fails_the_message(tmp_path):
    db_path = str(tmp_path / "inbox.db")
    conn = init_db(db_path)
    msg = _inbox_message(tmp_path, "a.md", "idempotency_key: two words\n")
    with pytest.raises(ValueError):
        _process_task(msg, tmp_path, conn, _insert_msg(db_path, str(msg.file_path)))
    conn.close()
    assert _task_count(db_path) == 0


def test_migration_backfills_legacy_rows_once_per_message(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    conn = init_db(db_path)
    msg_id = _insert_msg(db_path)
    # Simulate a pre-#378 table: no unique index, no keys, and a duplicate
    # task that the old check-then-insert let through.
    conn.execute("DROP INDEX idx_mta_idempotency_key")
    for approval_id in ("legacy-a", "legacy-b"):
        conn.execute(
            "INSERT INTO mailbox_task_approvals (id, mailbox_msg_id, task_body, sender, file_path, "
            "received_at, status, created_at, updated_at) "
            "VALUES (?, ?, 'Do work', 'alice', '/f.md', ?, 'pending_approval', ?, ?)",
            (approval_id, msg_id, _RECEIVED, _RECEIVED, _RECEIVED),
        )
    conn.commit()
    conn.close()

    conn = init_db(db_path)
    keys = dict(conn.execute("SELECT id, idempotency_key FROM mailbox_task_approvals"))
    approval_id, created = insert_task_approval(
        conn, mailbox_msg_id=msg_id, task_body="Do work", sender="alice",
        file_path="/f.md", received_at=_RECEIVED,
    )
    conn.close()

    assert keys == {"legacy-a": f"mailbox-msg:{msg_id}", "legacy-b": None}
    assert (approval_id, created) == ("legacy-a", False)


_NO_MERGE_TASK = "Fix the bug. Do not merge the PR."


def test_task_and_merge_constraint_commit_together(svc):
    msg_id = _insert_msg(svc.db_path)
    task = svc.create_task(msg_id, _NO_MERGE_TASK, "alice", "/f.md", _RECEIVED)

    # Read through a separate connection: only committed state is visible.
    conn = sqlite3.connect(svc.db_path)
    try:
        forbidden = conn.execute(
            "SELECT merge_forbidden FROM mailbox_task_approvals WHERE id = ?", (task.id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert forbidden == 1


def test_merge_constraint_failure_rolls_back_the_task(svc, monkeypatch):
    """A queued task must never persist without its no-merge constraint."""
    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("constraint write failed")

    monkeypatch.setattr("modules.run_store.schema.record_merge_constraint", boom)
    msg_id = _insert_msg(svc.db_path)

    with pytest.raises(sqlite3.OperationalError):
        svc.create_task(msg_id, _NO_MERGE_TASK, "alice", "/f.md", _RECEIVED)

    assert _task_count(svc.db_path) == 0
    _dispatch_all(svc)
    svc.experiment_service.create_experiment.assert_not_called()

    # The key was not burned: a redelivery after the fault creates the task.
    monkeypatch.undo()
    task = svc.create_task(msg_id, _NO_MERGE_TASK, "alice", "/f.md", _RECEIVED)
    assert task.merge_forbidden == 1
    assert _task_count(svc.db_path) == 1


def test_inbox_merge_constraint_failure_leaves_no_task(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("constraint write failed")

    monkeypatch.setattr("modules.run_store.schema.record_merge_constraint", boom)
    db_path = str(tmp_path / "inbox.db")
    conn = init_db(db_path)
    path = tmp_path / "a.md"
    path.write_text(f"---\ntype: task\ncreated_by: alice\n---\n{_NO_MERGE_TASK}\n", encoding="utf-8")
    msg = MailboxMessage.load(path, tmp_path)

    _process_task(msg, tmp_path, conn, _insert_msg(db_path, str(msg.file_path)))
    conn.close()

    assert _task_count(db_path) == 0
