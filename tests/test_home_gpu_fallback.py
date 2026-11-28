"""Issue #761 (home-gpu part 3): deadline fallback to the VM path + savings.

All tests use a fake clock (a fixed ``NOW``) so expiry, the 24 h PC-off rule,
the night window and the savings window are deterministic.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import modules.home_gpu.jobs as jobs_module
import modules.local_llm_jobs as llj_module
import chat
import api
from fastapi.testclient import TestClient
from modules.home_gpu import fallback
from modules.home_gpu.collect import _ensure_schema
from modules.home_gpu.fallback import (
    BATCH_KINDS,
    FALLBACK,
    INTERACTIVE_KINDS,
    STATUS_EXPIRED,
    STATUS_FALLBACK,
    STATUS_HOME_GPU_EXPIRED,
    STATUS_QUEUED,
    enqueue_vm_local,
    expire_jobs,
    home_gpu_status,
    in_vm_window,
    pc_is_off,
    savings_summary,
    submit_job,
    vm_window,
    vm_window_end,
)
from modules.local_model_rates import LocalModelRates
from modules.mailbox import core as mailbox_core
from modules.run_store import init_db

# 12:00 UTC: outside the default 0-6 night window, inside no 24 h answer window.
NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hours_ago(n: float) -> str:
    return iso(NOW - timedelta(hours=n))


@pytest.fixture
def db(tmp_path):
    conn = init_db(str(tmp_path / "agent.db"))
    yield conn
    conn.close()


@pytest.fixture
def mailbox(tmp_path):
    repo = tmp_path / "mailbox"
    (repo / "inbox").mkdir(parents=True)
    return repo


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("NEXUS_HOME_GPU", "1")


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


def _insert_job(
    db: sqlite3.Connection,
    job_id: str,
    *,
    kind: str = "chat_completion",
    status: str = STATUS_QUEUED,
    submitted_at: str = "2026-08-13T00:00:00Z",
    deadline: str = "2026-08-13T06:00:00Z",
    answered_at: str | None = None,
    completion_tokens: int | None = None,
    seconds: float | None = None,
) -> None:
    _ensure_schema(db)
    db.execute(
        """
        INSERT INTO home_gpu_jobs
            (job_id, kind, status, submitted_at, deadline, answered_at,
             completion_tokens, seconds, input_sha256)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id, kind, status, submitted_at, deadline, answered_at,
            completion_tokens, seconds, "0" * 64,
        ),
    )
    db.commit()


def _row(db: sqlite3.Connection, job_id: str):
    return db.execute(
        "SELECT kind, status, submitted_at, deadline FROM home_gpu_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()


def _pc_off_setup(db: sqlite3.Connection) -> None:
    """One job answered 25 h ago: no answer in the last 24 h, work older than that."""
    _insert_job(
        db, "hg-old", status="answered",
        submitted_at=hours_ago(26), answered_at=hours_ago(25), deadline=hours_ago(20),
        completion_tokens=10, seconds=4.0,
    )


def _suppress_probe(db: sqlite3.Connection, now: datetime = NOW) -> None:
    """Pretend a liveness probe was just sent, so tests that assert "nothing is
    written" are not disturbed by the (intended) bounded recovery probe."""
    _ensure_schema(db)
    db.execute(
        """
        INSERT INTO service_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (fallback.PROBE_SETTING, iso(now), iso(now)),
    )
    db.commit()


# --- kind split ---------------------------------------------------------------


def test_batch_and_interactive_kind_split():
    assert BATCH_KINDS == frozenset({"hunk_replay", "bakeoff_prompt"})
    assert INTERACTIVE_KINDS == frozenset({"code_draft", "review_draft", "chat_completion"})
    assert BATCH_KINDS | INTERACTIVE_KINDS == jobs_module.ALLOWED_KINDS
    assert FALLBACK == "fallback"


# --- expire_jobs --------------------------------------------------------------


def test_expire_jobs_marks_a_row_at_its_deadline_expired(db):
    _insert_job(db, "hg-at", deadline=iso(NOW))                       # exactly at the deadline
    _insert_job(db, "hg-past", deadline=iso(NOW - timedelta(seconds=1)))
    _insert_job(db, "hg-future", deadline=iso(NOW + timedelta(seconds=1)))
    _insert_job(db, "hg-answered", status="answered", deadline=iso(NOW - timedelta(hours=1)),
                answered_at=hours_ago(1))

    assert expire_jobs(db, NOW) == 2

    assert _row(db, "hg-at")[1] == STATUS_EXPIRED
    assert _row(db, "hg-past")[1] == STATUS_EXPIRED
    assert _row(db, "hg-future")[1] == STATUS_QUEUED
    assert _row(db, "hg-answered")[1] == "answered"

    # Idempotent: a second run expires nothing more.
    assert expire_jobs(db, NOW) == 0


def test_expire_jobs_without_past_deadlines_touches_nothing(db):
    _insert_job(db, "hg-future", deadline=iso(NOW + timedelta(hours=6)))
    assert expire_jobs(db, NOW) == 0
    assert _row(db, "hg-future")[1] == STATUS_QUEUED


def test_expire_jobs_creates_the_table_when_missing(tmp_path):
    conn = init_db(str(tmp_path / "fresh.db"))
    try:
        assert expire_jobs(conn, NOW) == 0  # no home_gpu_jobs table yet
    finally:
        conn.close()


# --- PC-off detection ----------------------------------------------------------


def test_pc_is_off_false_on_empty_table(db):
    assert pc_is_off(db, NOW) is False


def test_pc_is_off_false_while_fresh_jobs_are_in_flight(db):
    _insert_job(db, "hg-fresh", submitted_at=hours_ago(1), deadline=iso(NOW + timedelta(hours=5)))
    assert pc_is_off(db, NOW) is False


def test_pc_is_off_true_after_24h_without_an_answer(db):
    _pc_off_setup(db)
    assert pc_is_off(db, NOW) is True
    # ... and just inside the window it is still on:
    _insert_job(db, "hg-recent", status="answered", submitted_at=hours_ago(24),
                answered_at=iso(NOW - timedelta(hours=23, minutes=59)),
                deadline=iso(NOW + timedelta(hours=1)))
    assert pc_is_off(db, NOW) is False


def test_pc_is_off_true_for_old_queued_work_that_never_answered(db):
    _insert_job(db, "hg-stuck", submitted_at=hours_ago(30), deadline=hours_ago(24))
    assert pc_is_off(db, NOW) is True


# --- night window ---------------------------------------------------------------


def test_vm_window_defaults_to_0_6_utc():
    assert vm_window() == (0, 6)
    assert in_vm_window(datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)) is True
    assert in_vm_window(datetime(2026, 8, 13, 5, 59, tzinfo=timezone.utc)) is True
    assert in_vm_window(datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc)) is False
    assert in_vm_window(NOW) is False  # 12:00 UTC


def test_vm_window_is_configurable_and_wraps_midnight(monkeypatch):
    monkeypatch.setenv("NEXUS_HOME_GPU_VM_HOURS", "22-6")
    assert vm_window() == (22, 6)
    assert in_vm_window(datetime(2026, 8, 13, 23, 0, tzinfo=timezone.utc)) is True
    assert in_vm_window(datetime(2026, 8, 13, 3, 0, tzinfo=timezone.utc)) is True
    assert in_vm_window(NOW) is False


def test_vm_window_malformed_falls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv("NEXUS_HOME_GPU_VM_HOURS", "nonsense")
    assert vm_window() == (0, 6)
    assert in_vm_window(datetime(2026, 8, 13, 3, 0, tzinfo=timezone.utc)) is True


# --- submit_job with fallback ----------------------------------------------------


def test_batch_job_goes_to_the_vm_queue_and_writes_nothing_to_the_mailbox(
    db, mailbox, enabled, monkeypatch
):
    _pc_off_setup(db)
    _suppress_probe(db)

    def raiser(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("the home-GPU mailbox path was called with the PC off")

    monkeypatch.setattr(jobs_module, "submit_job", raiser)

    result = submit_job(
        db, "hunk_replay", {"hunk": "@@ -1 +1 @@"}, mailbox_dir=mailbox, commit=True, now=NOW
    )

    assert result == FALLBACK
    assert list((mailbox / "inbox").iterdir()) == []  # nothing written
    kind, status, _, _ = _row(db, "hg-old")
    assert (kind, status) == ("chat_completion", "answered")  # old row untouched
    new = db.execute(
        "SELECT job_id, kind, status FROM home_gpu_jobs WHERE job_id != 'hg-old'"
    ).fetchall()
    assert len(new) == 1
    assert new[0][1:] == ("hunk_replay", STATUS_FALLBACK)
    # The batch job is on the VM local path, queued under the home_gpu role.
    vm_rows = db.execute(
        "SELECT id, status, role, prompt FROM local_llm_jobs"
    ).fetchall()
    assert len(vm_rows) == 1
    vm_id, vm_status, vm_role, vm_prompt = vm_rows[0]
    assert vm_status == "queued"
    assert vm_role == llj_module.ROLE_HOME_GPU == "home_gpu"
    envelope = json.loads(vm_prompt)
    assert envelope == {"home_gpu_kind": "hunk_replay", "payload": {"hunk": "@@ -1 +1 @@"}}
    assert vm_id


def test_interactive_job_is_marked_home_gpu_expired_and_dropped(
    db, mailbox, enabled, monkeypatch
):
    _pc_off_setup(db)
    _suppress_probe(db)

    def raiser(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("the home-GPU mailbox path was called with the PC off")

    monkeypatch.setattr(jobs_module, "submit_job", raiser)

    for kind in sorted(INTERACTIVE_KINDS):
        assert submit_job(db, kind, {"p": "x"}, mailbox_dir=mailbox, commit=False, now=NOW) == FALLBACK

    assert list((mailbox / "inbox").iterdir()) == []
    rows = db.execute(
        "SELECT kind, status FROM home_gpu_jobs WHERE job_id != 'hg-old' ORDER BY kind"
    ).fetchall()
    assert rows == [(k, STATUS_HOME_GPU_EXPIRED) for k in sorted(INTERACTIVE_KINDS)]
    # Nothing was put on the VM local path for interactive kinds.
    has_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'local_llm_jobs'"
    ).fetchone()
    if has_table:
        assert db.execute("SELECT COUNT(*) FROM local_llm_jobs").fetchone()[0] == 0


def test_paid_path_is_not_called_when_the_pc_is_off(db, mailbox, enabled, git_calls, monkeypatch):
    """The paid path never waits on a home-GPU job: with the PC off, submit_job
    returns at once and neither the mailbox submit nor any git/paid call runs."""
    _pc_off_setup(db)
    _suppress_probe(db)
    calls: list[tuple] = []

    def sentinel(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("paid/mailbox submit must not be called with the PC off")

    monkeypatch.setattr(jobs_module, "submit_job", sentinel)

    assert submit_job(db, "bakeoff_prompt", {"p": "x"}, mailbox_dir=mailbox,
                      commit=True, now=NOW) == FALLBACK
    assert calls == []
    assert git_calls == []
    assert list((mailbox / "inbox").iterdir()) == []


def test_submit_job_goes_to_the_home_gpu_when_the_pc_is_on(db, mailbox, enabled, git_calls):
    _insert_job(db, "hg-recent", status="answered", submitted_at=hours_ago(2),
                answered_at=hours_ago(1), deadline=iso(NOW + timedelta(hours=4)),
                completion_tokens=7, seconds=2.0)

    job_id = submit_job(db, "bakeoff_prompt", {"p": "x"}, mailbox_dir=mailbox,
                        commit=True, now=NOW)

    assert job_id.startswith("hg-")
    kind, status, submitted_at, deadline = _row(db, job_id)
    assert (kind, status) == ("bakeoff_prompt", STATUS_QUEUED)
    assert submitted_at == iso(NOW)
    files = list((mailbox / "inbox").iterdir())
    assert len(files) == 1 and files[0].name == f"{job_id}.md"
    text = files[0].read_text(encoding="utf-8")
    assert "kind: bakeoff_prompt" in text and f"job_id: {job_id}" in text
    # The row mirrors the mailbox submission (jobs.submit_job uses the real
    # clock for the deadline, the row must carry exactly that deadline).
    file_deadline = next(
        line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("deadline: ")
    )
    assert deadline == file_deadline
    assert [c[0] for c in git_calls] == ["commit", "push"]


def test_submit_job_unknown_kind_raises_and_writes_nothing(db, mailbox, enabled, git_calls):
    with pytest.raises(jobs_module.HomeGpuJobError):
        submit_job(db, "not_a_kind", {"p": "x"}, mailbox_dir=mailbox, commit=True, now=NOW)
    assert list((mailbox / "inbox").iterdir()) == []
    assert git_calls == []


def test_submit_job_expires_past_deadlines_before_deciding(db, mailbox, enabled, git_calls):
    # PC on (recent answer), but a stale queued row is past its deadline.
    _insert_job(db, "hg-recent", status="answered", submitted_at=hours_ago(2),
                answered_at=hours_ago(1), deadline=iso(NOW + timedelta(hours=4)))
    _insert_job(db, "hg-stale", submitted_at=hours_ago(30), deadline=hours_ago(24))

    job_id = submit_job(db, "chat_completion", {"p": "hi"}, mailbox_dir=mailbox,
                        commit=False, now=NOW)

    assert job_id.startswith("hg-")
    assert _row(db, "hg-stale")[1] == STATUS_EXPIRED


def test_injected_enqueue_vm_is_used_for_batch_fallback(db, mailbox, enabled, monkeypatch):
    _pc_off_setup(db)
    _suppress_probe(db)
    monkeypatch.setattr(jobs_module, "submit_job", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("mailbox path called")))
    seen: list[tuple] = []

    def fake_enqueue(conn, kind, payload):
        seen.append((conn, kind, payload))
        return "vm-job-1"

    result = submit_job(db, "bakeoff_prompt", {"p": 1}, mailbox_dir=mailbox,
                        commit=False, now=NOW, enqueue_vm=fake_enqueue)
    assert result == FALLBACK
    assert seen == [(db, "bakeoff_prompt", {"p": 1})]
    # The default VM table was not created when an enqueue is injected.
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'local_llm_jobs'"
    ).fetchone() is None


# --- the VM local path only claims home_gpu rows in the night window -------------


class _IdleClient:
    """A llama client that reports the server reachable and all slots free."""

    model = "test-model"

    async def is_available(self):
        return True

    async def busy_slots(self):
        return 0

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None, system=None):
        return "ok"


def _service_without_worker(monkeypatch, tmp_path):
    monkeypatch.setattr(llj_module.LocalLlmJobService, "_worker_loop", lambda self: None)
    return llj_module.LocalLlmJobService(
        db_path=str(tmp_path / "jobs.db"), llama_client=_IdleClient(), poll_interval=0.01
    )


def test_claim_skips_home_gpu_rows_outside_the_night_window(monkeypatch, tmp_path):
    service = _service_without_worker(monkeypatch, tmp_path)
    try:
        hg_job = service.submit("hg-prompt", role=llj_module.ROLE_HOME_GPU)
        normal = service.submit("normal-prompt", role=llj_module.ROLE_CONTROLLER)

        monkeypatch.setattr(llj_module, "_home_gpu_vm_window_active", lambda: False)
        # Outside the window only the ordinary job is claimable.
        assert service._claim_next_job() == normal
        # Retire the ordinary job; with only the home-GPU row left, the closed
        # window leaves nothing claimable.
        service._conn.execute(
            "UPDATE local_llm_jobs SET status = ? WHERE id = ?",
            (llj_module.STATUS_CANCELLED, normal),
        )
        service._conn.commit()
        assert service._claim_next_job() is None
        assert service.get(hg_job).status == llj_module.STATUS_QUEUED

        monkeypatch.setattr(llj_module, "_home_gpu_vm_window_active", lambda: True)
        assert service._claim_next_job() == hg_job
        assert service.get(hg_job).status == llj_module.STATUS_RUNNING
    finally:
        service.close()


def test_claim_reuses_the_shared_idle_check_for_home_gpu_rows(monkeypatch, tmp_path):
    """Inside the window a home-GPU row is claimed only while the VM is
    otherwise idle (server_state); otherwise ordinary queued work wins and
    the fallback row waits (#761)."""
    service = _service_without_worker(monkeypatch, tmp_path)
    try:
        hg_job = service.submit("hg-prompt", role=llj_module.ROLE_HOME_GPU)
        normal = service.submit("normal-prompt", role=llj_module.ROLE_CONTROLLER)

        monkeypatch.setattr(llj_module, "_home_gpu_vm_window_active", lambda: True)
        # Another local job is queued, so the server is not idle for the
        # fallback row (which is excluded from its own idle decision): the
        # ordinary job is claimed instead.
        assert service._claim_next_job() == normal
        service._conn.execute(
            "UPDATE local_llm_jobs SET status = ? WHERE id = ?",
            (llj_module.STATUS_CANCELLED, normal),
        )
        service._conn.commit()
        # Now idle: the home-GPU row itself is claimed.
        assert service._claim_next_job() == hg_job
        assert service.get(hg_job).status == llj_module.STATUS_RUNNING
    finally:
        service.close()


def test_claim_fails_closed_for_home_gpu_rows_when_the_window_check_raises(
    monkeypatch, tmp_path
):
    service = _service_without_worker(monkeypatch, tmp_path)
    try:
        hg_job = service.submit("hg-prompt", role=llj_module.ROLE_HOME_GPU)
        normal = service.submit("normal-prompt", role=llj_module.ROLE_CONTROLLER)

        def boom(*_args, **_kwargs):  # pragma: no cover - the check must fail
            raise RuntimeError("broken window config")

        monkeypatch.setattr(fallback, "in_vm_window", boom)
        # The gate fails closed for the fallback role ...
        assert llj_module._home_gpu_vm_window_active() is False
        # ... so only the ordinary job is claimable; home-GPU work waits.
        assert service._claim_next_job() == normal
        service._conn.execute(
            "UPDATE local_llm_jobs SET status = ? WHERE id = ?",
            (llj_module.STATUS_CANCELLED, normal),
        )
        service._conn.commit()
        assert service._claim_next_job() is None
        assert service.get(hg_job).status == llj_module.STATUS_QUEUED
    finally:
        service.close()


def test_run_requeues_a_home_gpu_job_when_the_window_closed_between_claim_and_run(
    monkeypatch, tmp_path
):
    """The closing boundary is enforced during execution: a fallback job whose
    window closed before it started is put back on the queue, not run."""
    service = _service_without_worker(monkeypatch, tmp_path)
    try:
        hg_job = service.submit("hg-prompt", role=llj_module.ROLE_HOME_GPU)
        monkeypatch.setattr(llj_module, "_home_gpu_vm_window_active", lambda: True)
        assert service._claim_next_job() == hg_job
        # The window is gone at run start (fail closed: unknown end = closed).
        monkeypatch.setattr(fallback, "vm_window_end", lambda now=None: None)
        service._run(hg_job)
        job = service.get(hg_job)
        assert job.status == llj_module.STATUS_QUEUED
        assert job.worker_id is None
        assert job.started_at is None
    finally:
        service.close()


def test_home_gpu_run_timeout_bounds_execution_by_the_window_end():
    at = lambda h, m=0: datetime(2026, 8, 13, h, m, tzinfo=timezone.utc)  # noqa: E731

    # Ordinary roles are never window-limited.
    assert llj_module._home_gpu_run_timeout("controller", 900.0, now=at(12)) == (900.0, False)
    # Inside the window the closing boundary bounds the call when it is tighter
    # than the watchdog (05:30 -> 06:00 leaves 1800 s of a 7200 s watchdog).
    assert llj_module._home_gpu_run_timeout(llj_module.ROLE_HOME_GPU, 7200.0, now=at(5, 30)) == (
        1800.0,
        True,
    )
    # When the watchdog is tighter than the remaining window, the watchdog wins.
    assert llj_module._home_gpu_run_timeout(llj_module.ROLE_HOME_GPU, 900.0, now=at(5, 30)) == (
        900.0,
        False,
    )
    # A short remaining window wins over the watchdog.
    assert llj_module._home_gpu_run_timeout(llj_module.ROLE_HOME_GPU, 7200.0, now=at(5, 59)) == (
        60.0,
        True,
    )
    # Outside the window: fail closed, do not run at all.
    assert llj_module._home_gpu_run_timeout(llj_module.ROLE_HOME_GPU, 7200.0, now=at(12)) == (
        0.0,
        True,
    )


def test_home_gpu_run_timeout_fails_closed_when_the_window_is_unknown(monkeypatch):
    def boom(now=None):
        raise RuntimeError("broken")

    monkeypatch.setattr(fallback, "vm_window_end", boom)
    at = datetime(2026, 8, 13, 3, 0, tzinfo=timezone.utc)
    assert llj_module._home_gpu_run_timeout(llj_module.ROLE_HOME_GPU, 900.0, now=at) == (0.0, True)


def test_server_state_ignores_parked_fallback_rows_but_counts_live_ones(db, monkeypatch):
    """A parked home-GPU row outside the night window must not suppress
    ordinary shadow replays during the day; inside the window (or running)
    it counts as busy, unless it is the row being considered."""
    from modules.local_shadow_replay import server_state

    enqueue_vm_local(db, "hunk_replay", {"hunk": "x"}, now=NOW)
    db_path = db.execute("PRAGMA database_list").fetchone()[2]

    monkeypatch.setattr(llj_module, "_home_gpu_vm_window_active", lambda: False)
    idle, _reason = server_state(_IdleClient(), db_path)
    assert idle is True  # parked row: day time, no inference slot held

    monkeypatch.setattr(llj_module, "_home_gpu_vm_window_active", lambda: True)
    idle, reason = server_state(_IdleClient(), db_path)
    assert idle is False and "local model jobs" in reason

    row_id = db.execute("SELECT id FROM local_llm_jobs").fetchone()[0]
    idle, _reason = server_state(_IdleClient(), db_path, exclude_job_id=row_id)
    assert idle is True  # the fallback job being considered spares itself


def test_enqueue_vm_local_creates_the_queue_table(db):
    job_id = enqueue_vm_local(db, "hunk_replay", {"hunk": "x"}, now=NOW)
    row = db.execute(
        "SELECT status, role, prompt, created_at FROM local_llm_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == "queued" and row[1] == "home_gpu"
    assert json.loads(row[2])["home_gpu_kind"] == "hunk_replay"
    assert row[3] == iso(NOW)


# --- savings_summary ---------------------------------------------------------------


def test_savings_summary_arithmetic_on_fixed_rows(db):
    _insert_job(db, "hg-a1", status="answered", submitted_at=hours_ago(30),
                answered_at=hours_ago(24), deadline=hours_ago(20),
                completion_tokens=100, seconds=40.0)
    _insert_job(db, "hg-a2", status="answered", submitted_at=hours_ago(50),
                answered_at=hours_ago(48), deadline=hours_ago(44),
                completion_tokens=300, seconds=120.0)
    # Answered long ago: outside the 7-day window, must not count.
    _insert_job(db, "hg-old-answer", status="answered", submitted_at=iso(NOW - timedelta(days=10)),
                answered_at=iso(NOW - timedelta(days=9)), deadline=iso(NOW - timedelta(days=9)),
                completion_tokens=999, seconds=999.0)
    _insert_job(db, "hg-exp", status=STATUS_EXPIRED, submitted_at=hours_ago(30),
                deadline=hours_ago(24))
    _insert_job(db, "hg-fb", status=STATUS_FALLBACK, submitted_at=hours_ago(2),
                deadline=iso(NOW + timedelta(hours=4)))
    _insert_job(db, "hg-drop", status=STATUS_HOME_GPU_EXPIRED, submitted_at=hours_ago(1),
                deadline=iso(NOW + timedelta(hours=5)))
    _insert_job(db, "hg-queued", status=STATUS_QUEUED, submitted_at=iso(NOW),
                deadline=iso(NOW + timedelta(hours=6)))

    summary = savings_summary(db, days=7, now=NOW, vm_rate=2.0)

    assert summary["days"] == 7
    assert summary["answered_jobs"] == 2
    assert summary["completion_tokens"] == 400
    assert summary["pc_seconds"] == 160.0
    assert summary["vm_rate_tokens_per_second"] == 2.0
    assert summary["vm_seconds_saved"] == 400 / 2.0  # tokens / vm_rate
    assert summary["total_jobs"] == 7
    assert summary["expired_or_fallback_jobs"] == 3
    assert summary["expired_or_fallback_share"] == 3 / 7
    assert summary["window_start"] == iso(NOW - timedelta(days=7))


def test_savings_summary_uses_the_latest_measured_vm_rate(db, tmp_path):
    _insert_job(db, "hg-a", status="answered", submitted_at=hours_ago(3),
                answered_at=hours_ago(1), deadline=iso(NOW + timedelta(hours=5)),
                completion_tokens=90, seconds=30.0)
    rates_path = tmp_path / "rates.json"
    rates = LocalModelRates(rates_path)
    assert rates.record({"predicted_n": 26, "predicted_ms": 10000.0}) is True   # 2.6 tok/s
    assert rates.record({"predicted_n": 30, "predicted_ms": 10000.0}) is True   # 3.0 tok/s
    assert rates.latest_rate("generation_tokens_per_second") == 3.0  # the latest figure

    summary = savings_summary(db, days=7, now=NOW, rates=LocalModelRates(rates_path))
    assert summary["vm_rate_tokens_per_second"] == 3.0
    assert summary["vm_seconds_saved"] == 90 / 3.0


def test_savings_summary_defaults_to_2_6_without_measurements(db, tmp_path):
    _insert_job(db, "hg-a", status="answered", submitted_at=hours_ago(3),
                answered_at=hours_ago(1), deadline=iso(NOW + timedelta(hours=5)),
                completion_tokens= 26, seconds=10.0)
    summary = savings_summary(db, days=7, now=NOW,
                              rates=LocalModelRates(tmp_path / "missing.json"))
    assert summary["vm_rate_tokens_per_second"] == 2.6
    assert summary["vm_seconds_saved"] == 26 / 2.6


def test_savings_summary_on_a_database_without_the_table(tmp_path):
    conn = init_db(str(tmp_path / "fresh.db"))
    try:
        summary = savings_summary(conn, now=NOW, vm_rate=2.0)
        assert summary["answered_jobs"] == 0
        assert summary["completion_tokens"] == 0
        assert summary["pc_seconds"] == 0.0
        assert summary["vm_seconds_saved"] == 0.0
        assert summary["total_jobs"] == 0
        assert summary["expired_or_fallback_share"] == 0.0
    finally:
        conn.close()


def test_savings_summary_rejects_bad_days_and_rate(db):
    with pytest.raises(ValueError):
        savings_summary(db, days=0, now=NOW)
    with pytest.raises(ValueError):
        savings_summary(db, now=NOW, vm_rate=0)


def test_latest_rate_rejects_unknown_keys(tmp_path):
    with pytest.raises(ValueError):
        LocalModelRates(tmp_path / "r.json").latest_rate("tokens_per_hour")


# --- /status -----------------------------------------------------------------------


def test_home_gpu_status_reports_savings_and_never_a_path(db, tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME_GPU", "1")
    monkeypatch.setenv("LOCAL_MODEL_RATES_PATH", str(tmp_path / "rates.json"))
    _insert_job(db, "hg-a", status="answered", submitted_at=hours_ago(3),
                answered_at=hours_ago(1), deadline=iso(NOW + timedelta(hours=5)),
                completion_tokens=52, seconds=20.0)
    db.commit()

    info = home_gpu_status(db_path := db.execute("PRAGMA database_list").fetchone()[2],
                           now=NOW)
    assert info["enabled"] is True
    assert info["available"] is True
    assert info["pc_off"] is False
    assert info["vm_window"]["spec"] == "0-6"
    assert info["vm_window"]["active"] is False  # NOW is 12:00 UTC
    assert info["savings"]["answered_jobs"] == 1
    assert info["savings"]["completion_tokens"] == 52
    assert info["savings"]["pc_seconds"] == 20.0
    assert info["savings"]["vm_seconds_saved"] == 52 / 2.6
    assert db_path not in json.dumps(info)


def test_home_gpu_status_on_a_missing_database(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_HOME_GPU", raising=False)
    info = home_gpu_status(tmp_path / "missing.db", now=NOW)
    assert info == {"enabled": False, "vm_window": info["vm_window"], "available": False}
    assert info["vm_window"]["spec"] == "0-6"


def test_build_status_and_the_status_endpoint_show_home_gpu(db, tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME_GPU", "1")
    monkeypatch.setenv("LOCAL_MODEL_RATES_PATH", str(tmp_path / "rates.json"))
    db_path = str(tmp_path / "agent.db")
    # The db fixture already opened this path; re-point the env at it. The
    # /status block uses the real clock, so the answered row must be recent in
    # real time (not the fake NOW) to fall inside the 7-day savings window.
    real_now = datetime.now(timezone.utc)

    def real_iso(moment: datetime) -> str:
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _insert_job(db, "hg-a", status="answered",
                submitted_at=real_iso(real_now - timedelta(hours=3)),
                answered_at=real_iso(real_now - timedelta(hours=1)),
                deadline=real_iso(real_now + timedelta(hours=5)),
                completion_tokens=100, seconds=40.0)
    monkeypatch.setenv("AGENT_DB_PATH", db_path)

    status = chat.build_status()
    assert status["home_gpu"]["enabled"] is True
    assert status["home_gpu"]["savings"]["answered_jobs"] == 1
    assert status["home_gpu"]["savings"]["vm_seconds_saved"] == 100 / 2.6

    body = TestClient(api.app).get("/status").json()
    assert body["home_gpu"]["enabled"] is True
    assert body["home_gpu"]["pc_off"] is False
    assert body["home_gpu"]["savings"]["completion_tokens"] == 100
    text = json.dumps(body)
    assert "AGENT_API_KEY" not in text
    assert "/agent" not in text
    assert "test-key" not in text


# --- canonical entry point: modules.home_gpu.submit_job is the fallback wrapper ---


def test_package_level_submit_job_is_the_fallback_wrapper():
    import modules.home_gpu as hg

    assert hg.submit_job is fallback.submit_job
    assert hg.submit_mailbox_job is jobs_module.submit_job


def test_canonical_entry_point_diverts_to_the_vm_path_when_the_pc_is_off(
    db, mailbox, enabled, monkeypatch
):
    import modules.home_gpu as hg

    _pc_off_setup(db)
    _suppress_probe(db)

    def raiser(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("the mailbox path was called with the PC off")

    monkeypatch.setattr(jobs_module, "submit_job", raiser)

    result = hg.submit_job(
        db, "bakeoff_prompt", {"p": "x"}, mailbox_dir=mailbox, commit=False, now=NOW
    )
    assert result == FALLBACK
    assert list((mailbox / "inbox").iterdir()) == []
    assert db.execute(
        "SELECT status, role FROM home_gpu_jobs, (SELECT 'home_gpu' AS role) "
        "WHERE kind = 'bakeoff_prompt'"
    ).fetchone()[0] == STATUS_FALLBACK
    vm = db.execute("SELECT role FROM local_llm_jobs").fetchall()
    assert vm == [("home_gpu",)]


# --- the flag is checked before any schema or job mutation -----------------------


def test_submit_job_with_flag_off_raises_before_any_mutation(db, mailbox, monkeypatch):
    monkeypatch.delenv("NEXUS_HOME_GPU", raising=False)
    # Old rows that would read as "PC off" - with the flag off none of that
    # may cause a queue entry, a drop row or any other write.
    _insert_job(
        db, "hg-old", status="answered",
        submitted_at=hours_ago(26), answered_at=hours_ago(25), deadline=hours_ago(20),
    )

    with pytest.raises(jobs_module.HomeGpuDisabled):
        submit_job(db, "hunk_replay", {"hunk": "x"}, mailbox_dir=mailbox, commit=True, now=NOW)

    assert list((mailbox / "inbox").iterdir()) == []
    assert db.execute("SELECT COUNT(*) FROM home_gpu_jobs").fetchone()[0] == 1
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'local_llm_jobs'"
    ).fetchone() is None


def test_submit_job_with_flag_off_creates_no_schema_on_a_fresh_db(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_HOME_GPU", raising=False)
    conn = init_db(str(tmp_path / "fresh.db"))
    repo = tmp_path / "mailbox"
    (repo / "inbox").mkdir(parents=True)
    try:
        with pytest.raises(jobs_module.HomeGpuDisabled):
            submit_job(conn, "hunk_replay", {"hunk": "x"}, mailbox_dir=repo, commit=False, now=NOW)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'home_gpu_jobs'"
        ).fetchone() is None
        assert list((repo / "inbox").iterdir()) == []
    finally:
        conn.close()


# --- batch fallback: validate first, one transaction, idempotent ------------------


def test_batch_fallback_with_rejected_payload_writes_nothing(db, mailbox, enabled, monkeypatch):
    _pc_off_setup(db)
    _suppress_probe(db)

    def raiser(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("mailbox path called")

    monkeypatch.setattr(jobs_module, "submit_job", raiser)

    with pytest.raises(jobs_module.PayloadRejected):
        submit_job(
            db, "hunk_replay", {"url": "https://evil.example/x"},
            mailbox_dir=mailbox, commit=True, now=NOW,
        )

    assert list((mailbox / "inbox").iterdir()) == []
    assert db.execute(
        "SELECT COUNT(*) FROM home_gpu_jobs WHERE job_id != 'hg-old'"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'local_llm_jobs'"
    ).fetchone() is None  # the VM queue table was not even created


def test_batch_fallback_tracking_failure_rolls_back_the_vm_row(
    db, mailbox, enabled, monkeypatch
):
    """The VM-queue row and the tracking row commit in one transaction: a
    failing tracking insert must not leave runnable VM work behind, and a
    retry can enqueue it again cleanly."""
    _pc_off_setup(db)
    _suppress_probe(db)

    def broken_insert(*_args, **_kwargs):
        raise RuntimeError("tracking insert failed")

    real_insert = fallback._insert_tracking_row
    monkeypatch.setattr(fallback, "_insert_tracking_row", broken_insert)

    with pytest.raises(RuntimeError):
        submit_job(db, "hunk_replay", {"hunk": "x"}, mailbox_dir=mailbox,
                   commit=False, now=NOW)

    has_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'local_llm_jobs'"
    ).fetchone()
    if has_table:
        assert db.execute("SELECT COUNT(*) FROM local_llm_jobs").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM home_gpu_jobs WHERE job_id != 'hg-old'"
    ).fetchone()[0] == 0

    # A retry after the failure enqueues the VM row and the tracking row.
    monkeypatch.setattr(fallback, "_insert_tracking_row", real_insert)
    assert submit_job(db, "hunk_replay", {"hunk": "x"}, mailbox_dir=mailbox,
                      commit=False, now=NOW) == FALLBACK
    assert db.execute("SELECT COUNT(*) FROM local_llm_jobs").fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM home_gpu_jobs WHERE status = ?", (STATUS_FALLBACK,)
    ).fetchone()[0] == 1


def test_batch_fallback_enqueue_failure_leaves_no_tracking_row(db, mailbox, enabled):
    _pc_off_setup(db)
    _suppress_probe(db)

    def boom(_conn, _kind, _payload):
        raise RuntimeError("vm queue unavailable")

    with pytest.raises(RuntimeError):
        submit_job(
            db, "hunk_replay", {"hunk": "x"}, mailbox_dir=mailbox, commit=False,
            now=NOW, enqueue_vm=boom,
        )

    assert db.execute(
        "SELECT COUNT(*) FROM home_gpu_jobs WHERE job_id != 'hg-old'"
    ).fetchone()[0] == 0
    assert list((mailbox / "inbox").iterdir()) == []


def test_batch_fallback_is_idempotent_for_a_repeated_job_id(db, mailbox, enabled):
    _pc_off_setup(db)
    _suppress_probe(db)

    assert submit_job(db, "hunk_replay", {"hunk": "x"}, mailbox_dir=mailbox,
                      commit=False, now=NOW, job_id="hg-rep") == FALLBACK
    assert submit_job(db, "hunk_replay", {"hunk": "x"}, mailbox_dir=mailbox,
                      commit=False, now=NOW, job_id="hg-rep") == FALLBACK

    assert db.execute(
        "SELECT COUNT(*) FROM home_gpu_jobs WHERE job_id = 'hg-rep'"
    ).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM local_llm_jobs").fetchone()[0] == 1


# --- online branch: tracking row first, recoverable under the same job id ---------


def test_online_branch_failure_after_the_row_leaves_it_recoverable_under_the_same_id(
    db, mailbox, enabled, monkeypatch
):
    _insert_job(db, "hg-recent", status="answered", submitted_at=hours_ago(2),
                answered_at=hours_ago(1), deadline=iso(NOW + timedelta(hours=4)))
    real_submit = jobs_module.submit_job
    calls: list[str | None] = []

    def flaky(kind, payload, deadline_s, *, mailbox_dir, commit, job_id=None, now=None):
        calls.append(job_id)
        if len(calls) == 1:
            raise jobs_module.HomeGpuJobError("mailbox commit failed")
        return real_submit(kind, payload, deadline_s, mailbox_dir=mailbox_dir,
                           commit=commit, job_id=job_id, now=now)

    monkeypatch.setattr(jobs_module, "submit_job", flaky)

    with pytest.raises(jobs_module.HomeGpuJobError):
        submit_job(db, "chat_completion", {"p": "x"}, mailbox_dir=mailbox,
                   commit=False, now=NOW, job_id="hg-retry")

    # The row was written before the publish attempt and survives the failure.
    assert _row(db, "hg-retry")[1] == STATUS_QUEUED
    assert list((mailbox / "inbox").iterdir()) == []

    # A retry republishes under the same job id and does not duplicate the row.
    result = submit_job(db, "chat_completion", {"p": "x"}, mailbox_dir=mailbox,
                        commit=False, now=NOW, job_id="hg-retry")
    assert result == "hg-retry"
    assert calls == ["hg-retry", "hg-retry"]
    assert db.execute(
        "SELECT COUNT(*) FROM home_gpu_jobs WHERE job_id = 'hg-retry'"
    ).fetchone()[0] == 1
    assert [p.name for p in (mailbox / "inbox").iterdir()] == ["hg-retry.md"]


def test_online_branch_tracking_failure_never_publishes_to_the_mailbox(
    db, mailbox, enabled, monkeypatch
):
    """Row first, mailbox second: when the tracking insert fails, not a single
    mailbox file exists, so the collector can never see an unknown job."""
    _insert_job(db, "hg-recent", status="answered", submitted_at=hours_ago(2),
                answered_at=hours_ago(1), deadline=iso(NOW + timedelta(hours=4)))

    def raiser(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("mailbox published before the tracking row")

    monkeypatch.setattr(jobs_module, "submit_job", raiser)

    def broken_insert(*_args, **_kwargs):
        raise RuntimeError("tracking insert failed")

    monkeypatch.setattr(fallback, "_insert_tracking_row", broken_insert)

    with pytest.raises(RuntimeError):
        submit_job(db, "chat_completion", {"p": "x"}, mailbox_dir=mailbox,
                   commit=False, now=NOW, job_id="hg-new")

    assert list((mailbox / "inbox").iterdir()) == []
    assert db.execute(
        "SELECT COUNT(*) FROM home_gpu_jobs WHERE job_id = 'hg-new'"
    ).fetchone()[0] == 0


# --- bounded liveness probe and recovery (PC-off must not be forever) --------------


def test_pc_off_sends_at_most_one_probe_per_interval(db, mailbox, enabled):
    _pc_off_setup(db)

    assert submit_job(db, "hunk_replay", {"hunk": "x"}, mailbox_dir=mailbox,
                      commit=False, now=NOW) == FALLBACK

    # Exactly one probe went to the mailbox; the caller's own job did not.
    files = list((mailbox / "inbox").iterdir())
    assert len(files) == 1
    probe_rows = db.execute(
        "SELECT job_id, kind, status FROM home_gpu_jobs WHERE kind = ? AND job_id != 'hg-old'",
        (fallback.PROBE_KIND,),
    ).fetchall()
    assert len(probe_rows) == 1
    probe_id, _, probe_status = probe_rows[0]
    assert probe_status == STATUS_QUEUED
    assert files[0].name == f"{probe_id}.md"
    assert f"job_id: {probe_id}" in files[0].read_text(encoding="utf-8")
    # The caller's batch job still went to the VM queue as fallback.
    assert db.execute(
        "SELECT status FROM home_gpu_jobs WHERE kind = 'hunk_replay'"
    ).fetchone()[0] == STATUS_FALLBACK
    assert db.execute("SELECT COUNT(*) FROM local_llm_jobs").fetchone()[0] == 1

    # A second submission inside the interval sends no new probe.
    assert submit_job(db, "code_draft", {"p": "x"}, mailbox_dir=mailbox,
                      commit=False, now=NOW) == FALLBACK
    assert len(list((mailbox / "inbox").iterdir())) == 1
    assert db.execute(
        "SELECT COUNT(*) FROM home_gpu_jobs WHERE kind = ? AND job_id != 'hg-old'",
        (fallback.PROBE_KIND,),
    ).fetchone()[0] == 1

    # After the interval a new probe may go out.
    later = NOW + timedelta(hours=fallback.DEFAULT_PC_PROBE_HOURS, minutes=1)
    assert submit_job(db, "code_draft", {"p": "x"}, mailbox_dir=mailbox,
                      commit=False, now=later) == FALLBACK
    assert db.execute(
        "SELECT COUNT(*) FROM home_gpu_jobs WHERE kind = ? AND job_id != 'hg-old'",
        (fallback.PROBE_KIND,),
    ).fetchone()[0] == 2
    assert len(list((mailbox / "inbox").iterdir())) == 2


def test_probe_interval_is_configurable(db, mailbox, enabled, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME_GPU_PC_PROBE_HOURS", "1")
    _pc_off_setup(db)
    submit_job(db, "code_draft", {"p": "x"}, mailbox_dir=mailbox, commit=False, now=NOW)
    assert db.execute(
        "SELECT COUNT(*) FROM home_gpu_jobs WHERE kind = ? AND job_id != 'hg-old'",
        (fallback.PROBE_KIND,),
    ).fetchone()[0] == 1
    later = NOW + timedelta(hours=1, minutes=1)
    submit_job(db, "code_draft", {"p": "x"}, mailbox_dir=mailbox, commit=False, now=later)
    assert db.execute(
        "SELECT COUNT(*) FROM home_gpu_jobs WHERE kind = ? AND job_id != 'hg-old'",
        (fallback.PROBE_KIND,),
    ).fetchone()[0] == 2


def test_answered_probe_recovers_the_pc(db, mailbox, enabled):
    _pc_off_setup(db)
    assert submit_job(db, "hunk_replay", {"hunk": "x"}, mailbox_dir=mailbox,
                      commit=False, now=NOW) == FALLBACK
    assert pc_is_off(db, NOW) is True

    # The PC comes back and answers the probe.
    probe_id = db.execute(
        "SELECT job_id FROM home_gpu_jobs WHERE kind = ? AND job_id != 'hg-old'",
        (fallback.PROBE_KIND,),
    ).fetchone()[0]
    db.execute(
        "UPDATE home_gpu_jobs SET status = 'answered', answered_at = ? WHERE job_id = ?",
        (iso(NOW), probe_id),
    )
    db.commit()
    assert pc_is_off(db, NOW) is False

    # Ordinary submissions go to the GPU again at once.
    job_id = submit_job(db, "chat_completion", {"p": "hi"}, mailbox_dir=mailbox,
                        commit=False, now=NOW)
    assert job_id.startswith("hg-") and job_id != probe_id
    assert (mailbox / "inbox" / f"{job_id}.md").exists()
    assert _row(db, job_id)[1] == STATUS_QUEUED


def test_probe_failure_does_not_break_the_fallback_return(db, mailbox, enabled, monkeypatch):
    _pc_off_setup(db)

    def raiser(*_args, **_kwargs):
        raise RuntimeError("mailbox repo gone")

    monkeypatch.setattr(jobs_module, "submit_job", raiser)
    # The probe fails, but the caller still gets its immediate fallback.
    assert submit_job(db, "code_draft", {"p": "x"}, mailbox_dir=mailbox,
                      commit=False, now=NOW) == FALLBACK
    assert db.execute(
        "SELECT status FROM home_gpu_jobs WHERE kind = 'code_draft'"
    ).fetchone()[0] == STATUS_HOME_GPU_EXPIRED


# --- UTC normalization of the window and the fake clock ----------------------------


def test_vm_window_uses_utc_not_the_local_hour_of_an_offset_aware_clock():
    plus2 = timezone(timedelta(hours=2))
    # 08:00+02:00 is 06:00 UTC: outside the 0-6 window.
    assert in_vm_window(datetime(2026, 8, 13, 8, 0, tzinfo=plus2)) is False
    # 07:30+02:00 is 05:30 UTC: inside.
    assert in_vm_window(datetime(2026, 8, 13, 7, 30, tzinfo=plus2)) is True
    # A naive value is read as UTC.
    assert in_vm_window(datetime(2026, 8, 13, 3, 0)) is True
    assert in_vm_window(datetime(2026, 8, 13, 12, 0)) is False


def test_vm_window_end_returns_the_closing_boundary_in_utc(monkeypatch):
    at = lambda h, m=0: datetime(2026, 8, 13, h, m, tzinfo=timezone.utc)  # noqa: E731
    assert vm_window_end(at(0)) == at(6)
    assert vm_window_end(at(5, 59)) == at(6)
    assert vm_window_end(at(12)) is None  # outside the window
    # An offset-aware clock is normalized first: 07:30+02:00 == 05:30 UTC.
    assert vm_window_end(datetime(2026, 8, 13, 7, 30, tzinfo=timezone(timedelta(hours=2)))) == at(6)
    # A window wrapping past midnight closes at 06:00 the next day.
    monkeypatch.setenv("NEXUS_HOME_GPU_VM_HOURS", "22-6")
    assert vm_window_end(at(23, 30)) == datetime(2026, 8, 14, 6, 0, tzinfo=timezone.utc)
    assert vm_window_end(at(3)) == at(6)
    assert vm_window_end(at(12)) is None


def test_run_fails_a_home_gpu_job_when_the_window_closes_mid_execution(monkeypatch, tmp_path):
    """A fallback job admitted just before the window closes is stopped at the
    closing boundary during execution and fails as window_closed."""
    import asyncio

    service = _service_without_worker(monkeypatch, tmp_path)
    try:
        hg_job = service.submit("hg-prompt", role=llj_module.ROLE_HOME_GPU)
        monkeypatch.setattr(llj_module, "_home_gpu_vm_window_active", lambda: True)
        assert service._claim_next_job() == hg_job

        class SlowClient(_IdleClient):
            async def complete(self, **_kwargs):
                await asyncio.sleep(5)
                return "late"

        service.llama = SlowClient()
        # The window ends 0.05 s from now, far below the 900 s watchdog.
        monkeypatch.setattr(
            fallback, "vm_window_end",
            lambda now=None: datetime.now(timezone.utc) + timedelta(seconds=0.05),
        )
        service._run(hg_job)
        job = service.get(hg_job)
        assert job.status == llj_module.STATUS_FAILED
        assert job.error_kind == llj_module.ERROR_KIND_WINDOW_CLOSED
        assert "window closed" in (job.error or "")
    finally:
        service.close()
