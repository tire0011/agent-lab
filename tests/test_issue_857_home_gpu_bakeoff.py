"""Issue #857: home-gpu bake-off lane.

``--via home-gpu`` submits one ``bakeoff_prompt`` job per case through
``modules.home_gpu.submit_job`` and writes no bake-off rows; ``--collect``
scores the answered jobs with the VM checker into ``host='home-gpu'`` rows.

Also pins the review blocker of the first attempt: the shared checker must
judge the VM worker against the text it was given (the *reduced* text for a
symbol unit in a file over 16 KiB), never against ``unit.start_text``.

The real-git tests build a throwaway repo (base + merged commit) and a real
experiment row; the merged tests run for real with pytest in a worktree.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from modules import home_gpu
from modules import local_shadow_replay as lsr
from modules import local_shadow_replay_hunks as lsh
from modules.home_gpu.fallback import STATUS_EXPIRED, STATUS_QUEUED
from modules.local_model_bakeoff import insert_bakeoff_row, list_bakeoffs
from modules.mailbox import core as mailbox_core
from modules.run_store import init_db
from modules.run_store.schema import _migrate_local_model_bakeoffs_columns

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("local_model_bakeoff_script_857",
                                              ROOT / "scripts/local_model_bakeoff.py")
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


def make_repo(root, base_files, merged_files) -> tuple[str, str, str]:
    repo = str(Path(root) / "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "main")
    for rel, text in base_files.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    for rel, text in merged_files.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "merged shapes (#42)")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def shapes_repo(root):
    return make_repo(root, {"pkg/__init__.py": "", SHAPES: BASE_SHAPES, "README.md": "b\n"},
                     {SHAPES: MERGED_SHAPES, "tests/test_shapes.py": TEST_SHAPES})


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
def workspace(tmp_path, monkeypatch):
    db_path = str(tmp_path / "agent.db")
    init_db(db_path).close()
    fixture = tmp_path / "hunks.json"
    fixture.write_text(json.dumps([{"experiment_id": "exp-1"} for _ in range(10)]))
    mailbox = tmp_path / "mailbox"
    (mailbox / "inbox").mkdir(parents=True)
    monkeypatch.setenv(lsr.ENV_WORKTREE_DIR, str(tmp_path / "worktrees"))
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


@pytest.fixture
def repo(workspace):
    repo, base, merged = shapes_repo(workspace["tmp"])
    add_experiment(workspace["db"], "exp-1", merged=merged, base=base)
    return repo


def fake_payload(item, index):
    """A cheap, valid payload (no git) for the bookkeeping tests."""
    return {"bakeoff": {"experiment_id": item["experiment_id"], "index": index},
            "commit": "0" * 40, "file": SHAPES, "symbol": "area", "reduced": False,
            "prompt": f"case {index}", "file_text": BASE_SHAPES}


def _fixture(ws):
    return bakeoff._load_fixture(ws["fixture"], ws["db"])


def _submit(ws, **kwargs):
    kwargs.setdefault("payload_fn", fake_payload)
    return bakeoff.submit_home_gpu_jobs(_fixture(ws), db_path=ws["db"],
                                        mailbox_dir=ws["mailbox"], commit=False, **kwargs)


def _inbox(mailbox) -> list[tuple[dict, dict]]:
    out = []
    for path in sorted((Path(mailbox) / "inbox").glob("*.md")):
        meta, body = mailbox_core.parse_front_matter(path.read_text(encoding="utf-8"))
        out.append((meta, json.loads(body)))
    return out


def _rows(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _jobs(db_path):
    return {r["job_id"]: r for r in _rows(
        db_path, "SELECT * FROM home_gpu_jobs WHERE kind = 'bakeoff_prompt'")}


def _cases(db_path):
    return {r["case_index"]: r for r in _rows(
        db_path, "SELECT * FROM local_model_bakeoff_home_gpu_cases ORDER BY created_at")}


def _write_reply(mailbox, job_id, answer, *, model="pc-7b", prompt=100, completion=50,
                 seconds=10.0):
    outbox = Path(mailbox) / "outbox"
    outbox.mkdir(exist_ok=True)
    (outbox / f"reply-{job_id}.md").write_text(
        "---\n"
        f"reply_to: {job_id}\nstatus: done\nmodel: {model}\nprompt_tokens: {prompt}\n"
        f"completion_tokens: {completion}\nseconds: {seconds}\n"
        "---\n"
        f"{answer}",
        encoding="utf-8",
    )


def _collect(ws, repo_root=None, **kwargs):
    return bakeoff.collect_home_gpu_bakeoffs(
        db_path=ws["db"], repo_root=repo_root or str(ws["tmp"]), mailbox_dir=ws["mailbox"],
        **kwargs)


def ok_score(payload, answer):
    return {"status": "scored", "applied": True, "compiled": True, "tests_passed": True,
            "has_tests": True, "error": None}


# ---- blocker 1: the VM checker compares against the worker's own start text ----


def _filler(n=400):
    return "".join(f"\n\ndef filler_{i}(x):\n    return x + {i}  # padding padding padding\n"
                   for i in range(n))


class _Worker:
    def __init__(self, text=None):
        self.text = text

    def run(self, *, working_dir, **_):
        if self.text is not None:
            _write(working_dir, SHAPES, self.text)
        return SimpleNamespace(success=True, error=None, timed_out=False, usage=None)


def _vm_score(repo, unit, old, new, text, *, test_runner=None, db_path=None):
    """The real VM ``_score_unit`` with a worker that writes *text* (None: no change)."""
    tests = lsh.unit_test_files(repo, new, lsr.merged_test_changes(repo, old, new), unit.file)
    module = lsh.module_name(unit.file)
    runner = test_runner or (lambda wt, t, timeout: lsh.run_unit_tests(wt, t, module, timeout))
    worktree = lsr._create_worktree(repo, new, db_path=db_path)
    try:
        return lsh._score_unit(repo, worktree, unit, old=old, new=new, title="t", summary="s",
                               client=None, worker_factory=lambda _c: _Worker(text),
                               tests=tests, test_runner=runner, baseline={})
    finally:
        lsr.remove_worktree(repo, worktree)


def _passing_runner(wt, tests, timeout):
    return lsr.MergedTestRun(1, 1, None)


@pytest.fixture
def large_repo(tmp_path, monkeypatch):
    """A >16 KiB module; the merge adds a brand-new top-level function ``added``."""
    monkeypatch.setenv(lsr.ENV_WORKTREE_DIR, str(tmp_path / "worktrees"))
    base = "import math\n" + _filler()
    merged = base + "\n\ndef added(r):\n    return math.pi * r\n"
    repo, old, new = make_repo(tmp_path, {"pkg/__init__.py": "", SHAPES: base},
                               {SHAPES: merged})
    units = lsh.hunk_units(repo, old, new)
    assert len(units) == 1
    unit = units[0]
    start, reduced = lsh.worker_start_text(unit)
    assert reduced and start != unit.start_text  # the worker sees the reduced file
    return repo, old, new, unit, start


def test_reduced_unit_left_untouched_is_not_applied(large_repo):
    repo, old, new, unit, start = large_repo
    outcome = _vm_score(repo, unit, old, new, None, test_runner=_passing_runner)
    # The reduced file lacks the new symbol: splice fails. The worker changed
    # nothing, so this is *not* an applied patch (the bug said True).
    assert outcome.applied is False
    merged_changes = lsh._change_set(unit.start_text, unit.merged_text)
    assert outcome.overlap == lsh.changed_line_overlap(merged_changes, set())
    assert "has no" in outcome.error


def test_reduced_unit_broken_answer_is_applied_and_overlap_uses_reduced_start(large_repo):
    repo, old, new, unit, start = large_repo
    broken = start + "\ndef added(r:\n"
    outcome = _vm_score(repo, unit, old, new, broken, test_runner=_passing_runner)
    assert outcome.applied is True
    merged_changes = lsh._change_set(unit.start_text, unit.merged_text)
    assert outcome.overlap == lsh.changed_line_overlap(
        merged_changes, lsh._change_set(start, broken))
    assert "does not parse" in outcome.error


def test_reduced_unit_correct_answer_is_applied_and_compiles(large_repo):
    repo, old, new, unit, start = large_repo
    answer = start + "\n\ndef added(r):\n    return math.pi * r\n"
    outcome = _vm_score(repo, unit, old, new, answer, test_runner=_passing_runner)
    assert outcome.applied is True and outcome.compiled is True
    assert outcome.overlap == 1.0


def test_score_unit_text_matches_vm_score_unit_on_reduced_unit(large_repo):
    """The home-GPU entry point scores the same text exactly like the VM replay."""
    repo, old, new, unit, start = large_repo
    for text in (start, start + "\ndef added(r:\n",
                 start + "\n\ndef added(r):\n    return math.pi * r\n"):
        vm = _vm_score(repo, unit, old, new, text, test_runner=_passing_runner)
        worktree = lsr._create_worktree(repo, new)
        try:
            pc = lsh.score_unit_text(worktree, unit, text, start_text=start, tests=[],
                                     test_runner=_passing_runner)
        finally:
            lsr.remove_worktree(repo, worktree)
        for field in ("applied", "compiled", "has_tests", "tests_passed", "overlap", "error"):
            assert getattr(pc, field) == getattr(vm, field), field


def test_vm_baseline_note_kept_per_unit_and_tests_run_once(large_repo):
    repo, old, new, unit, start = large_repo
    calls = []

    def failing(wt, tests, timeout):
        calls.append(tests)
        return lsr.MergedTestRun(1, 0, None)  # total=1, passed=0

    worktree = lsr._create_worktree(repo, new)
    try:
        baseline: dict = {}
        outcomes = [lsh._score_unit(repo, worktree, unit, old=old, new=new, title="t",
                                    summary="s", client=None,
                                    worker_factory=lambda _c: _Worker(None),
                                    tests=["tests/x.py"], test_runner=failing,
                                    baseline=baseline) for _ in range(2)]
    finally:
        lsr.remove_worktree(repo, worktree)
    assert len(calls) == 1  # the baseline is cached across units of the file
    for outcome in outcomes:  # ... but every unit still records why it is untested
        assert outcome.has_tests is False
        assert "not tested: merged tests fail on the merged tree (0/1)" in outcome.error


# ---- --via home-gpu ----------------------------------------------------------


def test_via_home_gpu_writes_one_inbox_file_per_case_and_no_rows(workspace, enabled, repo):
    report = bakeoff.submit_home_gpu_jobs(_fixture(workspace), db_path=workspace["db"],
                                          repo_root=repo, mailbox_dir=workspace["mailbox"],
                                          commit=False)
    assert report["submitted"] == 10 and report["errors"] == 0
    files = _inbox(workspace["mailbox"])
    assert len(files) == 10
    assert {meta["kind"] for meta, _ in files} == {"bakeoff_prompt"}
    assert {meta["to"] for meta, _ in files} == {"home-gpu"}
    assert sorted(p["bakeoff"]["index"] for _, p in files) == list(range(10))
    payload = files[0][1]
    unit = bakeoff.resolve_case("exp-1", repo_root=repo, db_path=workspace["db"])["unit"]
    assert payload["file"] == SHAPES and payload["symbol"] == unit.symbol
    assert payload["file_text"] == lsh.worker_start_text(unit)[0] == unit.start_text
    assert payload["reduced"] is False
    assert "Edit only pkg/shapes.py" in payload["prompt"]
    assert "complete new content" in payload["prompt"]
    assert {j["status"] for j in _jobs(workspace["db"]).values()} == {STATUS_QUEUED}
    assert {c["status"] for c in _cases(workspace["db"]).values()} == {"submitted"}
    assert list_bakeoffs(50, db_path=workspace["db"]) == []


def test_via_home_gpu_through_main(workspace, enabled, no_git, repo, capsys):
    rc = bakeoff.main(["--via", "home-gpu", "--db-path", workspace["db"], "--repo-root", repo,
                       "--fixture", str(workspace["fixture"]),
                       "--mailbox-dir", str(workspace["mailbox"])])
    assert rc == 0
    assert len(_inbox(workspace["mailbox"])) == 10
    assert list_bakeoffs(50, db_path=workspace["db"]) == []
    report = json.loads(capsys.readouterr().out)
    assert report["via"] == "home-gpu" and report["submitted"] == 10


def test_via_home_gpu_with_flag_off_exits_non_zero_and_writes_nothing(workspace, monkeypatch,
                                                                      capsys):
    monkeypatch.delenv("NEXUS_HOME_GPU", raising=False)
    rc = bakeoff.main(["--via", "home-gpu", "--db-path", workspace["db"],
                       "--fixture", str(workspace["fixture"]),
                       "--mailbox-dir", str(workspace["mailbox"])])
    assert rc != 0
    assert "NEXUS_HOME_GPU=1" in capsys.readouterr().err
    assert list((workspace["mailbox"] / "inbox").glob("*.md")) == []
    assert _jobs(workspace["db"]) == {} and _cases(workspace["db"]) == {}
    with pytest.raises(home_gpu.HomeGpuDisabled, match="NEXUS_HOME_GPU"):
        _submit(workspace)
    assert _cases(workspace["db"]) == {}


def test_via_home_gpu_without_mailbox_exits_non_zero(workspace, enabled, monkeypatch, capsys):
    monkeypatch.delenv("AGENT_MAILBOX_REPO_PATH", raising=False)
    rc = bakeoff.main(["--via", "home-gpu", "--db-path", workspace["db"],
                       "--fixture", str(workspace["fixture"])])
    assert rc == 2 and "mailbox" in capsys.readouterr().err


def test_via_home_gpu_twice_does_not_double_the_jobs(workspace, enabled):
    first = _submit(workspace)
    second = _submit(workspace)
    assert first["submitted"] == 10
    assert second["submitted"] == 0 and second["already_pending"] == 10
    assert {j["job_id"] for j in second["jobs"]} == {j["job_id"] for j in first["jobs"]}
    assert len(_inbox(workspace["mailbox"])) == 10 and len(_jobs(workspace["db"])) == 10


def test_interrupted_submission_resumes_under_the_same_job_id(workspace, enabled):
    calls = []

    def dies_on_fourth(conn, kind, payload, deadline_s, **kw):
        calls.append(kw["job_id"])
        if len(calls) == 4:
            raise KeyboardInterrupt
        return home_gpu.submit_job(conn, kind, payload, deadline_s, **kw)

    with pytest.raises(KeyboardInterrupt):
        _submit(workspace, submit_fn=dies_on_fourth)
    cases = _cases(workspace["db"])
    assert sorted(cases) == [0, 1, 2, 3]
    assert cases[3]["status"] == "submitting" and cases[3]["job_id"] == calls[3]
    assert len(_inbox(workspace["mailbox"])) == 3
    assert list_bakeoffs(50, db_path=workspace["db"]) == []

    again = _submit(workspace)
    assert again["already_pending"] == 3 and again["submitted"] == 7
    resumed = next(j for j in again["jobs"] if j["index"] == 3)
    assert resumed["job_id"] == calls[3] and resumed.get("resumed") is True
    assert len(_inbox(workspace["mailbox"])) == 10 and len(_jobs(workspace["db"])) == 10
    assert {c["status"] for c in _cases(workspace["db"]).values()} == {"submitted"}


def test_crash_after_submit_job_republishes_instead_of_a_second_job(workspace, enabled):
    def submits_then_dies(conn, kind, payload, deadline_s, **kw):
        result = home_gpu.submit_job(conn, kind, payload, deadline_s, **kw)
        if payload["bakeoff"]["index"] == 5:
            raise KeyboardInterrupt  # before the case is marked submitted
        return result

    with pytest.raises(KeyboardInterrupt):
        _submit(workspace, submit_fn=submits_then_dies)
    job_id = _cases(workspace["db"])[5]["job_id"]
    assert _cases(workspace["db"])[5]["status"] == "submitting"
    again = _submit(workspace)
    assert next(j for j in again["jobs"] if j["index"] == 5)["job_id"] == job_id
    assert len(_jobs(workspace["db"])) == 10  # no second job for case 5


def test_failed_mailbox_commit_is_reported_and_resumed(workspace, enabled, capsys):
    def commit_fails(conn, kind, payload, deadline_s, **kw):
        if payload["bakeoff"]["index"] == 2:
            raise home_gpu.HomeGpuJobError("job file written but the mailbox commit failed")
        return home_gpu.submit_job(conn, kind, payload, deadline_s, **kw)

    report = _submit(workspace, submit_fn=commit_fails)
    assert report["errors"] == 1 and report["submitted"] == 9
    case = _cases(workspace["db"])[2]
    assert case["status"] == "submitting" and "commit failed" in case["error"]
    again = _submit(workspace)
    assert again["errors"] == 0 and again["submitted"] == 1
    assert next(j for j in again["jobs"] if j["index"] == 2)["job_id"] == case["job_id"]
    assert _cases(workspace["db"])[2]["error"] is None


def test_rejected_and_unresolvable_cases_are_reported_not_submitted(workspace, enabled):
    def payloads(item, index):
        if index == 0:
            raise ValueError("experiment exp-1 is not merged")
        payload = fake_payload(item, index)
        if index == 1:
            payload["file_text"] = "open('/etc/passwd')\n"  # absolute path: never published
        return payload

    report = _submit(workspace, payload_fn=payloads)
    assert report["fixture_errors"] == 1 and report["rejected"] == 1
    assert report["submitted"] == 8
    assert len(_inbox(workspace["mailbox"])) == 8 and sorted(_cases(workspace["db"])) == list(
        range(2, 10))


def test_expired_case_is_sent_again_but_fallback_crash_is_not_resubmitted(workspace, enabled):
    first = _submit(workspace)
    expired_id = first["jobs"][0]["job_id"]
    fallback_id = first["jobs"][1]["job_id"]
    conn = sqlite3.connect(workspace["db"])
    with conn:
        conn.execute("UPDATE home_gpu_jobs SET status = 'expired' WHERE job_id = ?", (expired_id,))
        conn.execute("UPDATE home_gpu_jobs SET status = 'fallback' WHERE job_id = ?",
                     (fallback_id,))
        # A crash right after the fallback left the case in 'submitting'.
        conn.execute("UPDATE local_model_bakeoff_home_gpu_cases SET status = 'submitting' "
                     "WHERE job_id = ?", (fallback_id,))
    conn.close()
    seen = []

    def record(conn, kind, payload, deadline_s, **kw):
        seen.append(kw["job_id"])
        return home_gpu.submit_job(conn, kind, payload, deadline_s, **kw)

    again = _submit(workspace, submit_fn=record)
    assert fallback_id not in seen and expired_id not in seen
    by_index = {j["index"]: j for j in again["jobs"]}
    assert by_index[0]["status"] == "submitted" and by_index[0]["job_id"] != expired_id
    statuses = {c["job_id"]: c["status"] for c in _rows(
        workspace["db"], "SELECT * FROM local_model_bakeoff_home_gpu_cases")}
    assert statuses[fallback_id] == "fallback"


# ---- --collect ---------------------------------------------------------------


def _stable(error):
    """A note without pytest's run-specific noise (addresses, temp paths, timings)."""
    if error is None:
        return None
    error = re.sub(r"0x[0-9a-f]+", "0x", error)
    error = re.sub(r"/\S*/", "/<tmp>/", error)
    return re.sub(r"in \d+\.\d+s", "in Ns", error)


def test_collect_scores_answers_exactly_like_the_vm_replay(workspace, enabled, repo):
    submitted = bakeoff.submit_home_gpu_jobs(_fixture(workspace), db_path=workspace["db"],
                                             repo_root=repo, mailbox_dir=workspace["mailbox"],
                                             commit=False)
    ids = [j["job_id"] for j in submitted["jobs"]]
    answers = {
        ids[0]: MERGED_SHAPES,                                  # passes
        ids[1]: "```python\n" + MERGED_SHAPES + "```\n",       # fenced, passes
        ids[2]: BASE_SHAPES,                                    # unchanged
        ids[3]: "def area(r:\n",                                # does not parse
        ids[4]: MERGED_SHAPES.replace("math.pi * r * r", "3 * r * r"),  # compiles, fails
    }
    for job_id, text in answers.items():
        _write_reply(workspace["mailbox"], job_id, text)
    conn = sqlite3.connect(workspace["db"])
    with conn:  # one job runs out of time: skipped, never a failure
        conn.execute("UPDATE home_gpu_jobs SET status = 'expired' WHERE job_id = ?", (ids[9],))
    conn.close()

    report = _collect(workspace, repo_root=repo)  # also runs collect_replies
    assert report["scored"] == 5 and report["written"] == 1
    waiting = {w["job_id"]: w["status"] for w in report["waiting"]}
    assert waiting == {**{i: STATUS_QUEUED for i in ids[5:9]}, ids[9]: STATUS_EXPIRED}

    rows = list_bakeoffs(50, db_path=workspace["db"])
    assert len(rows) == 1
    row = rows[0]
    assert row["host"] == "home-gpu" and row["model"] == "pc-7b"
    assert row["status"] == "completed" and row["hunks_total"] == 5
    assert row["seconds"] == 50.0
    assert row["prompt_tokens_per_second"] == pytest.approx(10.0)
    assert row["output_tokens_per_second"] == pytest.approx(5.0)
    assert row["winner"] is False and row["recommend_switch"] is False

    # Parity: the real VM _score_unit, its worker writing the same text.
    case = bakeoff.resolve_case("exp-1", repo_root=repo, db_path=workspace["db"])
    hunks = {h["job_id"]: h for h in row["details"]["hunks"]}
    for job_id, text in answers.items():
        vm = _vm_score(repo, case["unit"], case["old"], case["new"],
                       bakeoff.answer_file_text(text), db_path=workspace["db"])
        for field in ("applied", "compiled", "has_tests", "tests_passed", "overlap",
                      "tests_total", "tests_ok"):
            assert hunks[job_id][field] == getattr(vm, field), (job_id, field)
        assert _stable(hunks[job_id]["error"]) == _stable(vm.error), job_id
    assert row["passing_hunks"] == 2 and row["hunks_applied"] == 4
    assert row["hunks_compiled"] == 3

    jobs = _jobs(workspace["db"])
    assert all(jobs[i]["status"] == STATUS_QUEUED for i in ids[5:9])
    again = _collect(workspace, repo_root=repo)
    assert again["scored"] == 0 and again["written"] == 0
    assert len(list_bakeoffs(50, db_path=workspace["db"])) == 1


def test_missing_replies_stay_queued_and_write_no_rows(workspace, enabled):
    _submit(workspace)
    report = _collect(workspace, score_fn=ok_score)
    assert report["written"] == 0 and len(report["waiting"]) == 10
    assert {j["status"] for j in _jobs(workspace["db"]).values()} == {STATUS_QUEUED}
    assert list_bakeoffs(50, db_path=workspace["db"]) == []


def test_late_replies_are_collected_by_a_later_run(workspace, enabled):
    ids = [j["job_id"] for j in _submit(workspace)["jobs"]]
    _write_reply(workspace["mailbox"], ids[0], "x")
    assert _collect(workspace, score_fn=ok_score)["scored"] == 1
    _write_reply(workspace["mailbox"], ids[1], "y")
    assert _collect(workspace, score_fn=ok_score)["scored"] == 1
    rows = list_bakeoffs(50, db_path=workspace["db"])
    assert [r["hunks_total"] for r in rows] == [1, 1]
    assert {h["job_id"] for r in rows for h in r["details"]["hunks"]} == {ids[0], ids[1]}


def test_collect_crash_while_writing_rows_writes_nothing(workspace, enabled, monkeypatch):
    ids = [j["job_id"] for j in _submit(workspace)["jobs"]]
    _write_reply(workspace["mailbox"], ids[0], "a", model="m-a")
    _write_reply(workspace["mailbox"], ids[1], "b", model="m-b")
    real = bakeoff.insert_bakeoff_row
    calls = []

    def dies_on_second(conn, row, **kw):
        calls.append(row["model"])
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(conn, row, **kw)

    monkeypatch.setattr(bakeoff, "insert_bakeoff_row", dies_on_second)
    with pytest.raises(KeyboardInterrupt):
        _collect(workspace, score_fn=ok_score)
    assert list_bakeoffs(50, db_path=workspace["db"]) == []  # the first row rolled back too
    assert all(c["collected_at"] is None for c in _cases(workspace["db"]).values())

    monkeypatch.setattr(bakeoff, "insert_bakeoff_row", real)
    report = _collect(workspace, score_fn=ok_score)
    assert report["written"] == 2
    assert sorted(r["model"] for r in list_bakeoffs(50, db_path=workspace["db"])) == ["m-a", "m-b"]


def test_collect_interrupted_while_scoring_writes_nothing(workspace, enabled):
    ids = [j["job_id"] for j in _submit(workspace)["jobs"]]
    for job_id in ids[:3]:
        _write_reply(workspace["mailbox"], job_id, "a")

    def dies(payload, answer):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _collect(workspace, score_fn=dies)
    assert list_bakeoffs(50, db_path=workspace["db"]) == []
    assert _collect(workspace, score_fn=ok_score)["scored"] == 3


def test_concurrent_collect_never_writes_a_case_twice(workspace, enabled):
    ids = [j["job_id"] for j in _submit(workspace)["jobs"]]
    for job_id in ids[:2]:
        _write_reply(workspace["mailbox"], job_id, "a")

    def other_run_wins(payload, answer):
        # A second --collect finishes ids[0] while this one is still scoring.
        conn = sqlite3.connect(workspace["db"])
        with conn:
            conn.execute("UPDATE local_model_bakeoff_home_gpu_cases SET collected_at = 'x' "
                         "WHERE job_id = ?", (ids[0],))
        conn.close()
        return ok_score(payload, answer)

    report = _collect(workspace, score_fn=other_run_wins)
    assert {s["job_id"]: s["reason"] for s in report["skipped"]} == {ids[0]: "already collected"}
    rows = list_bakeoffs(50, db_path=workspace["db"])
    assert len(rows) == 1 and [h["job_id"] for h in rows[0]["details"]["hunks"]] == [ids[1]]


def test_scoring_error_is_recorded_like_the_vm_path_and_others_still_score(workspace, enabled):
    ids = [j["job_id"] for j in _submit(workspace)["jobs"]]
    for job_id in ids[:2]:
        _write_reply(workspace["mailbox"], job_id, "a")

    def flaky(payload, answer):
        if payload["bakeoff"]["index"] == 0:
            raise ValueError("experiment exp-1 is not merged")
        return ok_score(payload, answer)

    report = _collect(workspace, score_fn=flaky)
    assert report["scored"] == 2
    hunks = {h["index"]: h for h in list_bakeoffs(50, db_path=workspace["db"])[0]["details"]["hunks"]}
    assert hunks[0]["status"] == "fixture_error" and hunks[1]["tests_passed"] is True


def test_tampered_or_missing_answer_file_is_skipped_not_scored(workspace, enabled):
    ids = [j["job_id"] for j in _submit(workspace)["jobs"]]
    for job_id in ids[:2]:
        _write_reply(workspace["mailbox"], job_id, "a")
    conn = init_db(workspace["db"])
    home_gpu.collect_replies(workspace["mailbox"], conn, workspace["tmp"] / "state")
    conn.commit()
    conn.close()
    jobs = _jobs(workspace["db"])
    Path(jobs[ids[0]]["answer_path"]).write_text("changed", encoding="utf-8")
    Path(jobs[ids[1]]["answer_path"]).unlink()
    report = _collect(workspace, score_fn=ok_score)
    assert report["written"] == 0
    reasons = {s["job_id"]: s["reason"] for s in report["skipped"]}
    assert "sha256" in reasons[ids[0]] and "unreadable" in reasons[ids[1]]
    assert all(c["collected_at"] is None for c in _cases(workspace["db"]).values())


def test_collect_with_flag_off_scores_already_answered_jobs_without_reading_outbox(
        workspace, enabled, monkeypatch):
    ids = [j["job_id"] for j in _submit(workspace)["jobs"]]
    _write_reply(workspace["mailbox"], ids[0], "a")
    conn = init_db(workspace["db"])
    home_gpu.collect_replies(workspace["mailbox"], conn, workspace["tmp"] / "state")
    conn.commit()
    conn.close()
    _write_reply(workspace["mailbox"], ids[1], "b")
    monkeypatch.delenv("NEXUS_HOME_GPU")
    report = _collect(workspace, score_fn=ok_score)
    assert report["scored"] == 1  # ids[1] was never collected from the outbox
    assert _jobs(workspace["db"])[ids[1]]["status"] == STATUS_QUEUED


def test_collect_through_main(workspace, enabled, capsys):
    _submit(workspace)
    rc = bakeoff.main(["--collect", "--db-path", workspace["db"],
                       "--repo-root", str(workspace["tmp"]),
                       "--mailbox-dir", str(workspace["mailbox"])])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["written"] == 0


def test_answer_file_text_drops_one_outer_fence_only():
    assert bakeoff.answer_file_text("```python\nx = 1\n```") == "x = 1\n"
    assert bakeoff.answer_file_text("x = 1") == "x = 1\n"
    inner = "x = '''\n```\n'''\n"
    assert bakeoff.answer_file_text(inner) == inner


# ---- host column -------------------------------------------------------------


OLD_TABLE = """
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

BASE_ROW = {"created_at": "2026-08-13T00:00:00Z", "is_current": 0, "status": "completed",
            "hunks_total": 0, "hunks_applied": 0, "hunks_compiled": 0, "passing_hunks": 0,
            "local_too_slow": 0, "seconds": 0, "passing_hunks_per_hour": 0,
            "winner": 0, "recommend_switch": 0, "details_json": None, "error": None}


def test_host_column_migration_is_idempotent_and_defaults_to_vm(tmp_path):
    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(OLD_TABLE)
        conn.execute("INSERT INTO local_model_bakeoffs (run_id, created_at, model, status) "
                     "VALUES ('old-run', '2026-08-13T00:00:00Z', '7b.gguf', 'completed')")
    conn.close()
    init_db(db_path).close()
    init_db(db_path).close()
    assert [r["host"] for r in list_bakeoffs(10, db_path=db_path)] == ["vm"]
    conn = init_db(db_path)
    insert_bakeoff_row(conn, {"run_id": "r2", "model": "3b.gguf", **BASE_ROW,
                              "created_at": "2026-08-13T01:00:00Z"})
    insert_bakeoff_row(conn, {"run_id": "r3", "model": "pc-7b", **BASE_ROW,
                              "created_at": "2026-08-13T02:00:00Z", "host": "home-gpu"})
    conn.close()
    by_model = {r["model"]: r["host"] for r in list_bakeoffs(10, db_path=db_path)}
    assert by_model == {"7b.gguf": "vm", "3b.gguf": "vm", "pc-7b": "home-gpu"}


def test_host_migration_tolerates_a_concurrent_winner(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "race.db"))
    conn.execute(OLD_TABLE)
    conn.execute("ALTER TABLE local_model_bakeoffs ADD COLUMN host TEXT NOT NULL DEFAULT 'vm'")

    class StaleProbe:
        """The PRAGMA probe ran before the other process added the column."""

        def execute(self, sql, *args):
            if sql.startswith("PRAGMA"):
                return iter([])
            return conn.execute(sql, *args)

    _migrate_local_model_bakeoffs_columns(StaleProbe())  # no "duplicate column" error
    columns = [r[1] for r in conn.execute("PRAGMA table_info(local_model_bakeoffs)")]
    assert columns.count("host") == 1
    conn.close()


def test_insert_rejects_unknown_host_and_commit_false_leaves_the_transaction_open(tmp_path):
    db_path = str(tmp_path / "new.db")
    conn = init_db(db_path)
    with pytest.raises(ValueError, match="host"):
        insert_bakeoff_row(conn, {"run_id": "r", "model": "m", **BASE_ROW, "host": "laptop"})
    insert_bakeoff_row(conn, {"run_id": "r", "model": "m", **BASE_ROW}, commit=False)
    conn.rollback()
    conn.close()
    assert list_bakeoffs(10, db_path=db_path) == []


def test_case_table_creation_is_idempotent(tmp_path):
    db_path = str(tmp_path / "x.db")
    for _ in range(2):
        init_db(db_path).close()
    assert _rows(db_path, "SELECT * FROM local_model_bakeoff_home_gpu_cases") == []
    columns = {r["name"] for r in _rows(db_path, "PRAGMA table_info(local_model_bakeoff_home_gpu_cases)")}
    assert {"job_id", "input_sha256", "payload_json", "collected_at", "bakeoff_id"} <= columns
