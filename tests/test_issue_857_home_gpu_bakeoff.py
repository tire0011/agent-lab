"""Issue #857: home-gpu bake-off lane - ``--via home-gpu`` submits one
``bakeoff_prompt`` job per case, ``--collect`` scores the answered jobs with
the VM checker into ``host='home-gpu'`` bake-off rows.

The submission tests use a temp mailbox with ``NEXUS_HOME_GPU=1``. The
collection test builds a real throwaway git repo (base + merged commit) and a
real experiment row, answers some jobs with fixture replies through
:func:`modules.home_gpu.collect.collect_replies`, and checks that the written
rows match :func:`modules.local_shadow_replay_hunks.check_unit_text` - the VM
scorer - run on the same answer text.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest

from modules import local_shadow_replay as lsr
from modules import local_shadow_replay_hunks as lsh
from modules.home_gpu import collect_replies, submit_job
from modules.home_gpu.collect import _ensure_schema
from modules.home_gpu.fallback import STATUS_EXPIRED, STATUS_QUEUED
from modules.local_model_bakeoff import insert_bakeoff_row, list_bakeoffs
from modules.mailbox import core as mailbox_core
from modules.run_store import init_db

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("local_model_bakeoff_script", ROOT / "scripts/local_model_bakeoff.py")
assert SPEC and SPEC.loader
bakeoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bakeoff)

SHAPES = "pkg/shapes.py"
BASE_SHAPES = "import math\n\n\ndef area(r):\n    return 0\n\n\ndef perimeter(r):\n    return 0\n"
MERGED_SHAPES = (
    "import math\n\n\ndef area(r):\n    return math.pi * r * r\n\n\n"
    "def perimeter(r):\n    return 2 * math.pi * r\n"
)
TEST_SHAPES = (
    "import math\n\nfrom pkg import shapes\n\n\n"
    "def test_area():\n    assert shapes.area(1) == math.pi\n\n\n"
    "def test_perimeter():\n    assert shapes.perimeter(1) == 2 * math.pi\n"
)
GOAL = "GitHub Issue #42: shapes compute real values"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_repo(root) -> tuple[str, str, str]:
    """A temp git repo with a base and a merged commit; returns (repo, base, merged)."""
    repo = str(Path(root) / "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "pkg/__init__.py", "")
    _write(repo, SHAPES, BASE_SHAPES)
    _write(repo, "README.md", "base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, SHAPES, MERGED_SHAPES)
    _write(repo, "tests/test_shapes.py", TEST_SHAPES)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "merged shapes (#42)")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def add_experiment(db_path, experiment_id, *, merged, base,
                   ended_at="2026-11-01T00:00:00+00:00"):
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO experiments (id, goal, status, created_at, implementation_provider, "
            "base_commit_sha) VALUES (?, ?, 'succeeded', ?, 'codex', ?)",
            (experiment_id, GOAL, ended_at, base),
        )
        conn.execute(
            "INSERT INTO experiment_adoptions (id, experiment_id, status, ended_at, "
            "integrated_commit_sha, created_at, updated_at) VALUES (?, ?, 'adopted', ?, ?, ?, ?)",
            (f"ad-{experiment_id}", experiment_id, ended_at, merged, ended_at, ended_at),
        )
    conn.close()


@pytest.fixture
def workspace(tmp_path):
    """A db, a 10-case fixture, a temp mailbox and a fake-git patch."""
    db_path = str(tmp_path / "agent.db")
    init_db(db_path).close()
    fixture = tmp_path / "hunks.json"
    fixture.write_text(json.dumps([{"experiment_id": "exp-1"} for _ in range(10)]))
    mailbox = tmp_path / "mailbox"
    (mailbox / "inbox").mkdir(parents=True)
    return {"tmp": tmp_path, "db": db_path, "fixture": fixture, "mailbox": mailbox}


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("NEXUS_HOME_GPU", "1")


@pytest.fixture
def no_git(monkeypatch):
    """Keep the real git out of the temp mailbox: commit/push succeed as no-ops."""
    monkeypatch.setattr(mailbox_core, "git_add_and_commit",
                        lambda repo, files, message, **_: True)
    monkeypatch.setattr(mailbox_core, "git_push", lambda repo: True)


def _inbox_jobs(mailbox) -> list[tuple[str, dict, dict]]:
    out = []
    for path in sorted((Path(mailbox) / "inbox").glob("*.md")):
        meta, body = mailbox_core.parse_front_matter(path.read_text(encoding="utf-8"))
        out.append((path.name, meta, json.loads(body)))
    return out


def _job_rows(db_path, kind="bakeoff_prompt"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM home_gpu_jobs WHERE kind = ? ORDER BY submitted_at, job_id", (kind,))]
    finally:
        conn.close()


def _write_reply(mailbox, job_id, answer, *, model="pc-7b", prompt=100, completion=50,
                 seconds=10.0, status="done"):
    outbox = Path(mailbox) / "outbox"
    outbox.mkdir(exist_ok=True)
    (outbox / f"reply-{job_id}.md").write_text(
        "---\n"
        f"reply_to: {job_id}\n"
        f"status: {status}\n"
        f"model: {model}\n"
        f"prompt_tokens: {prompt}\n"
        f"completion_tokens: {completion}\n"
        f"seconds: {seconds}\n"
        "---\n"
        f"{answer}\n",
        encoding="utf-8",
    )


# ---- --via home-gpu ----------------------------------------------------------


def test_via_home_gpu_writes_one_inbox_file_per_case_and_no_bakeoff_rows(workspace, enabled):
    report = bakeoff.submit_home_gpu_jobs(
        bakeoff._load_fixture(workspace["fixture"], workspace["db"]),
        db_path=workspace["db"], mailbox_dir=workspace["mailbox"], commit=False,
    )
    assert report["submitted"] == 10
    assert report["fallback"] == 0
    files = _inbox_jobs(workspace["mailbox"])
    assert len(files) == 10  # N inbox files, one per case
    assert {meta["kind"] for _, meta, _ in files} == {"bakeoff_prompt"}
    assert {meta["to"] for _, meta, _ in files} == {"home-gpu"}
    assert sorted(payload["index"] for _, _, payload in files) == list(range(10))
    assert all(payload["experiment_id"] == "exp-1" for _, _, payload in files)
    jobs = _job_rows(workspace["db"])
    assert len(jobs) == 10
    assert {j["status"] for j in jobs} == {STATUS_QUEUED}
    assert list_bakeoffs(50, db_path=workspace["db"]) == []  # 0 bake-off rows


def test_via_home_gpu_through_main_with_temp_mailbox(workspace, enabled, no_git, capsys):
    rc = bakeoff.main(["--via", "home-gpu", "--db-path", workspace["db"],
                       "--fixture", str(workspace["fixture"]),
                       "--mailbox-dir", str(workspace["mailbox"])])
    assert rc == 0
    assert len(_inbox_jobs(workspace["mailbox"])) == 10
    assert list_bakeoffs(50, db_path=workspace["db"]) == []
    report = json.loads(capsys.readouterr().out)
    assert report["via"] == "home-gpu" and report["submitted"] == 10


def test_via_home_gpu_with_flag_off_exits_non_zero_with_clear_message(workspace, monkeypatch, capsys):
    monkeypatch.delenv("NEXUS_HOME_GPU", raising=False)
    rc = bakeoff.main(["--via", "home-gpu", "--db-path", workspace["db"],
                       "--fixture", str(workspace["fixture"]),
                       "--mailbox-dir", str(workspace["mailbox"])])
    assert rc != 0
    err = capsys.readouterr().err
    assert "NEXUS_HOME_GPU=1" in err
    assert list((Path(workspace["mailbox"]) / "inbox").glob("*.md")) == []
    assert list_bakeoffs(50, db_path=workspace["db"]) == []
    # The library entry point raises the same clear error and writes nothing.
    conn = init_db(workspace["db"])
    try:
        with pytest.raises(Exception, match="NEXUS_HOME_GPU"):
            submit_job(conn, "bakeoff_prompt", {"experiment_id": "exp-1", "index": 0},
                       mailbox_dir=workspace["mailbox"], commit=False)
    finally:
        conn.close()


# ---- --collect ---------------------------------------------------------------


def test_collect_scores_answered_jobs_with_the_vm_checker(workspace, enabled, no_git, monkeypatch):
    tmp = workspace["tmp"]
    repo, base, merged = make_repo(tmp)
    add_experiment(workspace["db"], "exp-1", merged=merged, base=base)
    monkeypatch.setenv(lsr.ENV_WORKTREE_DIR, str(tmp / "worktrees"))

    submitted = bakeoff.submit_home_gpu_jobs(
        bakeoff._load_fixture(workspace["fixture"], workspace["db"]),
        db_path=workspace["db"], mailbox_dir=workspace["mailbox"], commit=False,
    )
    job_ids = [job["job_id"] for job in submitted["jobs"]]
    answered_ids = job_ids[:3]  # three fixture replies; the other seven stay queued
    for job_id in answered_ids:
        _write_reply(workspace["mailbox"], job_id, MERGED_SHAPES)

    conn = init_db(workspace["db"])
    try:
        assert collect_replies(workspace["mailbox"], conn, tmp / "state") == 3
    finally:
        conn.close()

    report = bakeoff.collect_home_gpu_bakeoffs(
        db_path=workspace["db"], repo_root=repo, fixture_path=workspace["fixture"],
        mailbox_dir=workspace["mailbox"],
    )
    assert report["scored"] == 3
    assert report["written"] == 1  # one model -> one normal bake-off row

    rows = list_bakeoffs(50, db_path=workspace["db"])
    assert len(rows) == 1
    row = rows[0]
    assert row["host"] == "home-gpu"
    assert row["model"] == "pc-7b"
    assert row["status"] == "completed"
    assert row["hunks_total"] == 3          # only the answered jobs, not all 10
    assert row["hunks_applied"] == 3
    assert row["hunks_compiled"] == 3
    assert row["passing_hunks"] == 3
    assert row["seconds"] == 30.0
    assert row["prompt_tokens_per_second"] == pytest.approx(300 / 30.0)
    assert row["output_tokens_per_second"] == pytest.approx(150 / 30.0)
    assert row["winner"] is False and row["recommend_switch"] is False

    # Parity with the VM scorer on the same text: check_unit_text run directly
    # on the same answer must agree with what --collect stored per job.
    conn = lsr._connect(workspace["db"])
    try:
        exp_row, adoption = lsr._load(conn, "exp-1")
    finally:
        conn.close()
    old, new = lsr.merged_range(repo, adoption["integrated_commit_sha"],
                                adoption["experiment_commit_sha"],
                                str(exp_row["base_commit_sha"]).strip())
    unit = lsh.hunk_units(repo, old, new)[0]
    test_changes = lsr.merged_test_changes(repo, old, new)
    tests = lsh.unit_test_files(repo, new, test_changes, unit.file)
    worktree = lsr._create_worktree(repo, new, db_path=workspace["db"])
    try:
        vm_outcome = lsh.check_unit_text(
            worktree, unit, MERGED_SHAPES, tests=tests,
            test_runner=lambda wt, t, timeout: lsh.run_unit_tests(
                wt, t, lsh.module_name(unit.file), timeout),
            baseline={}, notes=[])
    finally:
        lsr.remove_worktree(repo, worktree)
    details = row["details"]
    assert {j["job_id"] for j in details["jobs"]} == set(answered_ids)
    for job in details["jobs"]:
        outcome = job["outcome"]
        assert outcome["applied"] == vm_outcome.applied
        assert outcome["compiled"] == vm_outcome.compiled
        assert outcome["tests_passed"] == vm_outcome.tests_passed
        assert outcome["has_tests"] == vm_outcome.has_tests

    # The seven unanswered jobs stay queued - skipped, never scored as failures.
    jobs = {j["job_id"]: j for j in _job_rows(workspace["db"])}
    for job_id in job_ids[3:]:
        assert jobs[job_id]["status"] == STATUS_QUEUED
    assert len(rows) == 1 and rows[0]["hunks_total"] == 3

    # A repeated --collect does not duplicate the rows.
    again = bakeoff.collect_home_gpu_bakeoffs(
        db_path=workspace["db"], repo_root=repo, fixture_path=workspace["fixture"],
        mailbox_dir=workspace["mailbox"],
    )
    assert again["scored"] == 0
    assert len(list_bakeoffs(50, db_path=workspace["db"])) == 1


def test_collect_skips_queued_and_expired_jobs(workspace, enabled):
    db = workspace["db"]
    conn = init_db(db)
    _ensure_schema(conn)
    fixture = bakeoff._load_fixture(workspace["fixture"], db)
    payload = bakeoff.bakeoff_case_payload(fixture[0], 0, db)
    import hashlib
    digest = hashlib.sha256(
        __import__("modules.home_gpu", fromlist=["payload_body"]).payload_body(payload)
        .encode("utf-8")).hexdigest()
    rows = [
        ("hg-queued-1", STATUS_QUEUED, "2999-01-01T00:00:00Z", None),
        ("hg-expired-1", STATUS_EXPIRED, "2020-01-01T00:00:00Z", None),
        ("hg-answered-1", "answered", "2999-01-01T00:00:00Z", "some-answer"),
    ]
    with conn:
        for job_id, status, deadline, answer in rows:
            conn.execute(
                "INSERT INTO home_gpu_jobs (job_id, kind, status, submitted_at, deadline, "
                "input_sha256, answer_path, model, prompt_tokens, completion_tokens, seconds) "
                "VALUES (?, 'bakeoff_prompt', ?, '2026-08-13T00:00:00Z', ?, ?, ?, 'pc-7b', "
                "100, 50, 10.0)",
                (job_id, status, deadline,
                 digest if job_id == "hg-answered-1" else "0" * 64,
                 answer and str(workspace["tmp"] / f"{job_id}.txt")),
            )
        # the answered job's digest matches fixture case 0; give it an answer file
        answer_path = workspace["tmp"] / "hg-answered-1.txt"
        answer_path.write_text("some-answer", encoding="utf-8")
    conn.close()

    def fake_score(case, answer, job=None):
        assert case["experiment_id"] == "exp-1" and case["index"] == 0
        assert answer == "some-answer"
        return {"status": "scored", "applied": True, "compiled": True, "tests_passed": True,
                "error": None}

    report = bakeoff.collect_home_gpu_bakeoffs(
        db_path=db, fixture_path=workspace["fixture"], score_fn=fake_score,
    )
    assert report["scored"] == 1
    skipped_reasons = {s["job_id"]: s["reason"] for s in report["skipped"]}
    assert "hg-queued-1" not in skipped_reasons  # never even looked at: not answered
    assert "hg-expired-1" not in skipped_reasons
    rows = list_bakeoffs(50, db_path=db)
    assert len(rows) == 1
    assert rows[0]["host"] == "home-gpu"
    assert rows[0]["hunks_total"] == 1  # only the answered job; queued/expired are not failures
    assert rows[0]["passing_hunks"] == 1
    jobs = {j["job_id"]: j for j in _job_rows(db)}
    assert jobs["hg-queued-1"]["status"] == STATUS_QUEUED
    assert jobs["hg-expired-1"]["status"] == STATUS_EXPIRED


# ---- host column migration ---------------------------------------------------


def test_host_column_migration_is_idempotent_and_defaults_to_vm(tmp_path):
    db_path = str(tmp_path / "old.db")
    # A database whose local_model_bakeoffs predates the host column (#857).
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            """
            CREATE TABLE local_model_bakeoffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, created_at TEXT NOT NULL, model TEXT NOT NULL,
                is_current INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
                prompt_tokens_per_second REAL NULL, output_tokens_per_second REAL NULL,
                hunks_total INTEGER NOT NULL DEFAULT 0, hunks_applied INTEGER NOT NULL DEFAULT 0,
                hunks_compiled INTEGER NOT NULL DEFAULT 0, passing_hunks INTEGER NOT NULL DEFAULT 0,
                local_too_slow INTEGER NOT NULL DEFAULT 0, seconds REAL NOT NULL DEFAULT 0,
                passing_hunks_per_hour REAL NOT NULL DEFAULT 0,
                winner INTEGER NOT NULL DEFAULT 0, recommend_switch INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NULL, error TEXT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO local_model_bakeoffs (run_id, created_at, model, status) "
            "VALUES ('old-run', '2026-08-13T00:00:00Z', '7b.gguf', 'completed')"
        )
    conn.close()

    init_db(db_path).close()  # migration runs once
    init_db(db_path).close()  # and again: idempotent
    rows = list_bakeoffs(10, db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["host"] == "vm"  # pre-existing rows are VM rows

    conn = init_db(db_path)
    base = {"created_at": "2026-08-13T00:00:00Z", "is_current": 0, "status": "completed",
            "hunks_total": 0, "hunks_applied": 0, "hunks_compiled": 0, "passing_hunks": 0,
            "local_too_slow": 0, "seconds": 0, "passing_hunks_per_hour": 0,
            "winner": 0, "recommend_switch": 0, "details_json": None, "error": None}
    with conn:
        insert_bakeoff_row(conn, {"run_id": "r2", "model": "3b.gguf", **base,
                                  "created_at": "2026-08-13T01:00:00Z"})
        insert_bakeoff_row(conn, {"run_id": "r3", "model": "pc-7b", **base,
                                  "created_at": "2026-08-13T02:00:00Z", "host": "home-gpu"})
    conn.close()
    by_model = {r["model"]: r["host"] for r in list_bakeoffs(10, db_path=db_path)}
    assert by_model == {"7b.gguf": "vm", "3b.gguf": "vm", "pc-7b": "home-gpu"}


def test_fresh_database_has_host_column_with_vm_default(tmp_path):
    db_path = str(tmp_path / "new.db")
    conn = init_db(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(local_model_bakeoffs)")}
    assert "host" in columns
    with conn:
        insert_bakeoff_row(conn, {"run_id": "r", "created_at": "2026-08-13T00:00:00Z",
                                  "model": "7b.gguf", "is_current": 0, "status": "completed",
                                  "hunks_total": 0, "hunks_applied": 0, "hunks_compiled": 0,
                                  "passing_hunks": 0, "local_too_slow": 0, "seconds": 0,
                                  "passing_hunks_per_hour": 0, "winner": 0,
                                  "recommend_switch": 0, "details_json": None, "error": None})
    conn.close()
    assert list_bakeoffs(1, db_path=db_path)[0]["host"] == "vm"
