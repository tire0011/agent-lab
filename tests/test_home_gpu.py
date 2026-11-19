"""Home GPU batch lane (#743): one test group per review finding of experiment 30d143ed."""
import json
import os
import shutil
import subprocess
import sys
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from handlers import batch_job, home_gpu_worker
from modules.home_gpu import collect, fallback, jobs, sandbox
from modules.mailbox import core as mailbox_core
from modules.mailbox import poll_runner
from modules.mailbox.config import MailboxConfig
from modules.run_store import init_db

KEY = b"k" * 40
REAL_LS_REMOTE = jobs._ls_remote_main


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def lane(tmp_path, monkeypatch):
    """A public code repo, a bare mailbox remote with a Nexus and a PC clone, a key."""
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(name, "Lane Test")
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(name, "lane@example.invalid")
    monkeypatch.setenv("NEXUS_HOME_GPU", "1")
    key_file = tmp_path / "home-gpu.key"
    key_file.write_bytes(KEY)
    key_file.chmod(0o600)
    monkeypatch.setenv(batch_job.ENV_KEY_FILE, str(key_file))

    code = tmp_path / "code"
    code.mkdir()
    git(code, "init", "-q", "-b", "main")
    (code / "benchmarks").mkdir()
    (code / "benchmarks" / "chat.json").write_text(json.dumps(
        {"messages": [{"role": "user", "content": "Summarize what Nexus is."}], "max_tokens": 32}))
    (code / "benchmarks" / "schema.json").write_text(json.dumps({
        "messages": [{"role": "user", "content": "Return the status."}],
        "json_schema": {"type": "object", "required": ["status"],
                        "properties": {"status": {"type": "string"}}},
    }))
    (code / "benchmarks" / "draft.json").write_text(json.dumps(
        {"prompt": "Rename the helper.", "files": ["pkg/util.py"]}))
    (code / "benchmarks" / "review.json").write_text(json.dumps({"prompt": "Review the helper."}))
    (code / "pkg").mkdir()
    (code / "pkg" / "util.py").write_text("def helper():\n    return 1\n")
    git(code, "add", ".")
    git(code, "commit", "-q", "-m", "public fixtures")
    commit = git(code, "rev-parse", "HEAD")
    jobs._reset_canonical_cache()
    monkeypatch.setattr(jobs, "_ls_remote_main", lambda repo: commit)

    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(remote), str(seed))
    for folder in ("inbox", "outbox"):
        (seed / folder).mkdir()
        (seed / folder / ".gitkeep").write_text("")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "mailbox")
    git(seed, "push", "-q", "origin", "main")
    nexus = tmp_path / "mailbox"
    pc = tmp_path / "pc"
    git(tmp_path, "clone", "-q", str(remote), str(nexus))
    git(tmp_path, "clone", "-q", str(remote), str(pc))
    config = MailboxConfig.from_paths(code, nexus)
    return SimpleNamespace(config=config, db=str(tmp_path / "agent.db"), commit=commit,
                           code=code, pc=pc, nexus=nexus, tmp=tmp_path)


def submit(lane, path="benchmarks/chat.json", kind="chat_completion", deadline=21600):
    job_id = jobs.submit_job(kind, {"commit": lane.commit, "path": path}, deadline,
                             config=lane.config, db_path=lane.db)
    return collect.get_job(job_id, db_path=lane.db)


def nexus_poll(lane, *, sync=True):
    """The home GPU part of the regular Nexus mailbox poll, with the real sync."""
    published = poll_runner._home_gpu_before_sync(lane.config, lane.db, mailbox_core, "test")
    ok = sync and poll_runner._sync_mailbox_repo(
        mailbox_core, lane.nexus, ignored_untracked_prefixes=("outbox/",))
    if ok:
        poll_runner._home_gpu_after_sync(lane.config, lane.db, published, "test")
    return ok


class _Llama(BaseHTTPRequestHandler):
    answers: list = []
    requests: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, body))
        answer = type(self).answers.pop(0) if type(self).answers else "Nexus is an agent system."
        data = json.dumps({"model": "ornith-1.5-9b-q4_k_m",
                           "choices": [{"message": {"content": answer}}],
                           "usage": {"prompt_tokens": 12, "completion_tokens": 7},
                           "timings": {"predicted_per_second": 41.5}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def llama():
    _Llama.answers, _Llama.requests = [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Llama)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(port=server.server_address[1], cls=_Llama)
    server.shutdown()
    server.server_close()


# ---- (1) the PC worker's normal poll ------------------------------------------

def test_pc_poll_processes_a_batch_job_and_publishes_a_signed_reply(lane, llama):
    job = submit(lane)
    assert nexus_poll(lane)
    assert collect.get_job(job["job_id"], db_path=lane.db)["published_at"] is not None

    summary = home_gpu_worker.poll_once(lane.pc, key=KEY, port=llama.port)
    assert summary["answered"] == 1 and summary["pushed"]
    path, request = llama.cls.requests[0]
    assert path == "/v1/chat/completions"
    assert request["messages"] == [{"role": "user", "content": "Summarize what Nexus is."}]
    assert request["stream"] is False and request["max_tokens"] == 32

    assert nexus_poll(lane)
    done = collect.get_job(job["job_id"], db_path=lane.db)
    assert done["status"] == "answered" and done["answer_host"] == "home-gpu"
    assert done["answer"] == "Nexus is an agent system."
    assert done["model"] == "ornith-1.5-9b-q4_k_m" and done["tokens_per_second"] == 41.5
    assert done["check_status"] == "passed" and done["checked_at"] >= done["answered_at"]

    # Twice in a row: no second llama call, no second commit.
    head = git(lane.pc, "rev-parse", "HEAD")
    again = home_gpu_worker.poll_once(lane.pc, key=KEY, port=llama.port)
    assert again["answered"] == 0 and len(llama.cls.requests) == 1
    assert git(lane.pc, "rev-parse", "HEAD") == head


def test_pc_poll_recovers_a_reply_written_before_a_crash(lane, llama, monkeypatch):
    job = submit(lane)
    nexus_poll(lane)
    real_commit = home_gpu_worker._commit
    calls = []

    def crash(repo, paths):
        calls.append(paths)
        if paths and len(calls) == 2:
            raise RuntimeError("power loss")
        return real_commit(repo, paths)

    monkeypatch.setattr(home_gpu_worker, "_commit", crash)
    with pytest.raises(RuntimeError):
        home_gpu_worker.poll_once(lane.pc, key=KEY, port=llama.port)
    assert (lane.pc / "outbox" / f"{job['job_id']}.reply.md").is_file()
    monkeypatch.setattr(home_gpu_worker, "_commit", real_commit)
    assert not (lane.pc / ".git" / "home-gpu-worker.lock").exists()
    summary = home_gpu_worker.poll_once(lane.pc, key=KEY, port=llama.port)
    assert summary["pushed"] and len(llama.cls.requests) == 1
    nexus_poll(lane)
    assert collect.get_job(job["job_id"], db_path=lane.db)["status"] == "answered"


def test_pc_poll_ignores_other_recipients_and_unknown_types(lane, llama):
    (lane.pc / "inbox" / "note.md").write_text("---\ntype: task\nto: nexus\ncreated_by: owner\n---\nhi\n")
    (lane.pc / "inbox" / "odd.md").write_text("---\ntype: shell\nto: home-gpu\n---\nrm -rf /\n")
    summary = home_gpu_worker.poll_once(lane.pc, key=KEY, port=llama.port)
    assert summary["answered"] == 0 and summary["skipped"] == 1
    assert not llama.cls.requests
    registry = {}
    home_gpu_worker.register(registry)
    assert registry == {"batch_job": batch_job.handle}


# ---- (2) provenance and the public-data guard -----------------------------------

def test_provenance_uses_the_canonical_remote_not_local_refs(lane, monkeypatch):
    private = lane.tmp / "private"
    shutil.copytree(lane.code, private)
    (private / "secret.json").write_text('{"messages":[{"role":"user","content":"internal"}]}')
    git(private, "add", ".")
    git(private, "commit", "-q", "-m", "local only")
    local_only = git(private, "rev-parse", "HEAD")
    # A local origin/main and a public-looking origin URL prove nothing.
    git(private, "remote", "add", "origin", jobs.CANONICAL_URL + ".git")
    git(private, "update-ref", "refs/remotes/origin/main", local_only)
    with pytest.raises(ValueError):
        jobs.public_commit(private, local_only)
    jobs.public_commit(private, lane.commit)

    seen = []
    monkeypatch.setattr(jobs, "_git", lambda repo, *args, **kwargs: seen.append((args, kwargs)) or
                        f"{'a' * 40}\trefs/heads/main\n".encode())
    assert REAL_LS_REMOTE(Path(".")) == "a" * 40
    args, kwargs = seen[-1]
    assert args[-3:] == ("--", "https://github.com/tire0011/agent-lab", "refs/heads/main")
    assert kwargs == {"network": True}  # an outage is retryable, not a rejection
    with pytest.raises(ValueError):
        jobs.public_commit(Path("."), "HEAD")


@pytest.mark.parametrize("text", [
    "https://github.com/tire0011/agent-lab-private",
    "https://github.com/tire0011/agent-lab/../another",
    "https://github.com/tire0011/agent-lab?ref=x",
    "HTTP://github.com/tire0011/agent-lab",
    "https://other.example/repository",
    "//evil.example/payload",
    "see evil.co/collect",
    "git@github.com:other/repo.git",
    "file:///tmp/export",
    "file:/etc/hosts",
    "Read ../private.txt",
    "/home/company/notes.txt",
    "C:\\company\\notes.txt",
    "\\Users\\owner\\notes.txt",
    "\\\\fileserver\\share",
    "%APPDATA%\\tool",
    "~/notes",
    "$HOME",
    "token " + "sk-" + "a" * 16,
    "ghp_" + "A" * 24,
    "github_pat_" + "a" * 30,
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "AKIA" + "A" * 16,
])
def test_guard_rejects_external_data(text):
    with pytest.raises(ValueError):
        jobs.build_envelope("chat_completion",
                            {"messages": [{"role": "user", "content": text}]}, {})


@pytest.mark.parametrize("text", [
    "See https://github.com/tire0011/agent-lab/blob/main/README.md.",
    "github.com/tire0011/agent-lab/issues/743",
    "Edit modules/home_gpu/jobs.py and tests/test_home_gpu.py",
    "x = a // b; y = a/b",
])
def test_guard_accepts_public_project_text(text):
    jobs.build_envelope("chat_completion", {"messages": [{"role": "user", "content": text}]}, {})


def test_submission_rules(lane, monkeypatch):
    with pytest.raises(ValueError, match="committed"):
        jobs.submit_job("chat_completion", {"messages": []}, config=lane.config, db_path=lane.db)
    with pytest.raises(ValueError, match="replay"):
        submit(lane, kind="hunk_replay")
    with pytest.raises(ValueError, match="64 KiB"):
        jobs.build_envelope("chat_completion",
                            {"messages": [{"role": "user", "content": "é" * 33000}]}, {})
    monkeypatch.delenv(batch_job.ENV_KEY_FILE)
    with pytest.raises(batch_job.SigningKeyError):
        submit(lane)
    with closing(init_db(lane.db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM home_gpu_jobs").fetchone()[0] == 0


# ---- (3) authentication and isolation ---------------------------------------------

def _inbox_message(lane, job):
    nexus_poll(lane)
    git(lane.pc, "pull", "-q")
    return batch_job.split_message((lane.pc / "inbox" / f"{job['job_id']}.md").read_text())


def test_pc_refuses_unsigned_or_forged_jobs(lane, llama):
    job = submit(lane)
    meta, body = _inbox_message(lane, job)
    outbox = lane.pc / "outbox"
    for forged in (
        {**meta, "signature": "hmac-sha256:" + "0" * 64},
        {**meta, "kind": "code_draft"},
        {**meta, "deadline": str(float(meta["deadline"]) + 86400)},
        {key: value for key, value in meta.items() if key != "signature"},
    ):
        with pytest.raises(ValueError):
            batch_job.handle(forged, body, outbox, key=KEY, port=llama.port)
    tampered = body.replace("Summarize", "Exfiltrate")
    with pytest.raises(ValueError):
        batch_job.handle({**meta, "input_sha256": __import__("hashlib").sha256(
            tampered.encode()).hexdigest()}, tampered, outbox, key=KEY, port=llama.port)
    with pytest.raises(ValueError):
        batch_job.handle(meta, body, outbox, key=b"x" * 40, port=llama.port)
    assert not llama.cls.requests


def _push_reply(lane, text, job_id, *, commit=True):
    path = lane.pc / "outbox" / f"{job_id}.reply.md"
    path.write_text(text)
    if commit:
        git(lane.pc, "add", ".")
        git(lane.pc, "commit", "-q", "-m", "reply")
        git(lane.pc, "push", "-q")


def test_nexus_ignores_forged_and_uncommitted_replies(lane):
    job = submit(lane)
    nexus_poll(lane)
    git(lane.pc, "pull", "-q")
    good = dict(job_id=job["job_id"], digest=job["input_sha256"], model="m", prompt_tokens=1,
                completion_tokens=1, seconds=1.0, rate=1.0, answer="real answer")
    # An untracked reply in Nexus' own worktree never counts.
    (lane.nexus / "outbox").mkdir(exist_ok=True)
    (lane.nexus / "outbox" / f"{job['job_id']}.reply.md").write_text(
        batch_job.reply_message(KEY, **good))
    nexus_poll(lane)
    assert collect.get_job(job["job_id"], db_path=lane.db)["status"] == "queued"
    (lane.nexus / "outbox" / f"{job['job_id']}.reply.md").unlink()

    forged = batch_job.reply_message(b"z" * 40, **good)
    _push_reply(lane, forged, job["job_id"])
    nexus_poll(lane)
    assert collect.get_job(job["job_id"], db_path=lane.db)["status"] == "queued"

    swapped = batch_job.reply_message(KEY, **good).replace("real answer", "swapped answer")
    _push_reply(lane, swapped, job["job_id"])
    nexus_poll(lane)
    assert collect.get_job(job["job_id"], db_path=lane.db)["reply_seen_at"] is None

    _push_reply(lane, batch_job.reply_message(KEY, **good), job["job_id"])
    nexus_poll(lane)
    assert collect.get_job(job["job_id"], db_path=lane.db)["answer"] == "real answer"


def test_a_job_signature_is_not_a_reply_signature():
    meta = {"in_reply_to": "a" * 32, "input_sha256": "b" * 64, "model": "m", "prompt_tokens": "1",
            "completion_tokens": "1", "seconds": "1", "tokens_per_second": "1"}
    job_sig = batch_job._mac(KEY, batch_job._JOB_DOMAIN, batch_job._reply_fields(meta, "x"))
    with pytest.raises(ValueError):
        batch_job.verify_reply(KEY, {"type": "batch_reply", "to": "nexus", **meta, "signature": job_sig},
                               "x", job_id="a" * 32, digest="b" * 64)


@pytest.mark.skipif(sandbox.unavailable_reason() is not None, reason="bwrap not usable here")
def test_sandbox_hides_state_credentials_and_network(tmp_path):
    secret = Path.home() / ".nexus-sandbox-probe"
    work, scratch = tmp_path / "work", tmp_path / "scratch"
    work.mkdir()
    scratch.mkdir()
    (work / "ok.txt").write_text("visible")
    script = (
        "import os, socket, sys\n"
        f"assert open('ok.txt').read() == 'visible'\n"
        f"assert not os.path.exists({str(tmp_path / 'agent.db')!r})\n"
        f"assert not os.path.exists({str(Path.home())!r})\n"
        "assert 'NEXUS_HOME_GPU_KEY_FILE' not in os.environ\n"
        "try:\n socket.create_connection(('127.0.0.1', 22), timeout=1)\n"
        "except OSError:\n pass\nelse:\n sys.exit(5)\n"
    )
    (tmp_path / "agent.db").write_text("production")
    env = {"PATH": os.environ["PATH"]}
    done = subprocess.run(sandbox.command([sys.executable, "-c", script], str(work), str(scratch)),
                          env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert not secret.exists()


# ---- (4) deadlines do not depend on Git sync ---------------------------------------

def test_deadlines_advance_while_the_mailbox_is_down(lane):
    batch = submit(lane, deadline=60)
    draft = submit(lane, "benchmarks/review.json", "review_draft", deadline=60)
    shutil.rmtree(lane.tmp / "remote.git")  # mailbox outage
    nexus_poll(lane)  # publication commits locally; the sync fails
    assert not poll_runner._sync_mailbox_repo(mailbox_core, lane.nexus,
                                              ignored_untracked_prefixes=("outbox/",))
    collect.advance_deadlines(lane.db, now=max(batch["deadline"], draft["deadline"]))
    assert collect.get_job(batch["job_id"], db_path=lane.db)["status"] == "fallback"
    assert collect.get_job(batch["job_id"], db_path=lane.db)["error"] == "home_gpu_deadline"
    assert collect.get_job(draft["job_id"], db_path=lane.db)["error"] == "home_gpu_expired"


def test_jobs_past_or_near_their_deadline_are_not_published(lane):
    soon = submit(lane, deadline=collect.PUBLISH_MARGIN_SECONDS / 2)
    live = submit(lane)
    nexus_poll(lane)
    assert not (lane.nexus / "inbox" / f"{soon['job_id']}.md").exists()
    assert (lane.nexus / "inbox" / f"{live['job_id']}.md").exists()
    assert collect.get_job(soon["job_id"], db_path=lane.db)["published_at"] is None


def test_offline_pc_falls_back_and_a_returning_pc_is_noticed(lane, llama):
    first = submit(lane, deadline=3 * 86400)
    nexus_poll(lane)
    later = first["created_at"] + collect.OFFLINE_AFTER_SECONDS + 1
    collect.advance_deadlines(lane.db, now=later)
    assert collect.get_job(first["job_id"], db_path=lane.db)["error"] == "home_gpu_offline"
    fresh = submit(lane, deadline=3 * 86400)
    collect.advance_deadlines(lane.db, now=later)
    assert collect.get_job(fresh["job_id"], db_path=lane.db)["status"] == "queued"
    # The PC comes back and answers the fallback job before the VM started it.
    home_gpu_worker.poll_once(lane.pc, key=KEY, port=llama.port)
    nexus_poll(lane)
    assert collect.get_job(first["job_id"], db_path=lane.db)["status"] == "answered"


# ---- (5) interrupted inbox writes ---------------------------------------------------

def test_failed_inbox_write_leaves_no_temp_file_and_is_retried(lane, monkeypatch):
    job = submit(lane)
    real_replace = os.replace

    def broken(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    nexus_poll(lane)
    stored = collect.get_job(job["job_id"], db_path=lane.db)
    assert stored["status"] == "queued" and stored["publish_error"] == "OSError"
    assert stored["published_at"] is None
    assert list((lane.nexus / "inbox").iterdir()) == [lane.nexus / "inbox" / ".gitkeep"]
    monkeypatch.setattr(os, "replace", real_replace)
    assert nexus_poll(lane)
    stored = collect.get_job(job["job_id"], db_path=lane.db)
    assert stored["published_at"] is not None and stored["publish_error"] is None


def test_a_failed_commit_withdraws_the_file_and_is_retried(lane, monkeypatch):
    job = submit(lane)
    real_commit = mailbox_core.git_add_and_commit
    monkeypatch.setattr(mailbox_core, "git_add_and_commit", lambda *args: False)
    assert poll_runner._home_gpu_before_sync(lane.config, lane.db, mailbox_core, "test") == set()
    assert poll_runner._worktree_safe_for_mailbox_sync(mailbox_core, lane.nexus, ("outbox/",))
    assert collect.get_job(job["job_id"], db_path=lane.db)["publish_error"] == "commit_failed"
    # Restore only the commit; monkeypatch.undo() would also drop the fixture's git
    # identity, and CI runners have no global one.
    monkeypatch.setattr(mailbox_core, "git_add_and_commit", real_commit)
    assert nexus_poll(lane)
    stored = collect.get_job(job["job_id"], db_path=lane.db)
    assert stored["published_at"] is not None and stored["publish_error"] is None


def test_crash_leftovers_never_wedge_the_regular_mailbox(lane, monkeypatch):
    job = submit(lane)
    leftover = jobs.temp_path(lane.config, job["job_id"])
    leftover.write_text("half")
    # Even with the lane switched off, our temp file is cleaned before the sync.
    monkeypatch.setenv("NEXUS_HOME_GPU", "0")
    poll_runner._home_gpu_before_sync(lane.config, lane.db, mailbox_core, "test")
    assert not leftover.exists()
    assert poll_runner._worktree_safe_for_mailbox_sync(mailbox_core, lane.nexus, ("outbox/",))
    monkeypatch.setenv("NEXUS_HOME_GPU", "1")
    # A file staged by a commit that failed is regenerated and committed once.
    inbox = jobs.write_inbox(lane.config, (job["job_id"], job["kind"], job["deadline"],
                                           job["input_sha256"], job["payload"]), KEY)
    git(lane.nexus, "add", str(inbox))
    assert nexus_poll(lane)
    assert nexus_poll(lane)
    log = git(lane.nexus, "log", "--format=%s", "--", f"inbox/{job['job_id']}.md")
    assert log.splitlines() == ["Publish Nexus home GPU batch jobs"]


# ---- (6)-(8) fallback, checkers, retries ------------------------------------------------

class FakeService:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.submitted = []
        self.waited = []
        self.llama = SimpleNamespace(model="vm-qwen-7b")

    def submit(self, prompt, **kwargs):
        self.submitted.append((prompt, kwargs))
        return f"local-{len(self.submitted)}"

    async def wait(self, job_id):
        self.waited.append(job_id)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def vm(monkeypatch):
    from modules import local_llm_jobs

    state = SimpleNamespace(service=None)
    monkeypatch.setattr(local_llm_jobs, "acquire_local_llm_job_service",
                        lambda **kwargs: state.service)
    monkeypatch.setattr(local_llm_jobs, "release_local_llm_job_service", lambda service: None)
    monkeypatch.setattr(fallback.lsr, "server_state", lambda *args: (True, "idle"))
    monkeypatch.setattr(fallback.lsr, "default_replay_client", lambda: object())
    monkeypatch.setattr(collect, "night_window", lambda now=None: True)
    return state


def _overdue(lane, path="benchmarks/chat.json"):
    job = submit(lane, path, deadline=1)
    collect.advance_deadlines(lane.db, now=job["deadline"])
    return job


def test_vm_fallback_uses_the_job_service_and_then_the_checker(lane, vm):
    job = _overdue(lane, "benchmarks/schema.json")
    vm.service = FakeService(['{"status": "found"}'])
    assert fallback.advance_one(str(lane.code), lane.db)
    done = collect.get_job(job["job_id"], db_path=lane.db)
    assert done["answer_host"] == "vm" and done["model"] == "vm-qwen-7b"
    assert done["check_status"] == "passed"
    prompt, options = vm.service.submitted[0]
    assert prompt == "Return the status." and options["role"] == "home_gpu_fallback"
    assert options["response_format"]["type"] == "json_schema"
    assert options["max_tokens"] <= 1024
    assert not fallback.advance_one(str(lane.code), lane.db)

    bad = _overdue(lane, "benchmarks/schema.json")
    vm.service = FakeService(['{"other": 1}'])
    fallback.advance_one(str(lane.code), lane.db)
    failed = collect.get_job(bad["job_id"], db_path=lane.db)
    assert failed["answer"] and failed["check_status"] == "failed"
    assert failed["answered_at"] <= failed["checked_at"]


def test_vm_fallback_retries_transient_failures_and_stops_on_terminal_ones(lane, vm):
    from modules.local_llm_jobs import LocalLlmJobError

    job = _overdue(lane)
    vm.service = FakeService([LocalLlmJobError("timed out", kind="timeout", status="failed")])
    assert fallback.advance_one(str(lane.code), lane.db)
    retry = collect.get_job(job["job_id"], db_path=lane.db)
    assert retry["checked_at"] is None and retry["fallback_attempts"] == 1
    assert retry["fallback_job_id"] is None and retry["next_fallback_at"] > retry["created_at"]
    # Not before its back-off ...
    vm.service = FakeService([])
    assert not fallback.advance_one(str(lane.code), lane.db, now=retry["next_fallback_at"] - 1)
    # ... then a terminal model error ends it.
    vm.service = FakeService([LocalLlmJobError("bad", kind="model_error", status="failed")])
    assert fallback.advance_one(str(lane.code), lane.db, now=retry["next_fallback_at"] + 1)
    final = collect.get_job(job["job_id"], db_path=lane.db)
    assert final["check_status"] == "error" and final["error"] == "home_gpu_fallback_failed: model_error"


def test_vm_fallback_resumes_its_local_job_after_a_crash(lane, vm):
    job = _overdue(lane)
    vm.service = FakeService([KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        fallback.advance_one(str(lane.code), lane.db)
    assert collect.get_job(job["job_id"], db_path=lane.db)["fallback_job_id"] == "local-1"
    vm.service = FakeService(["late VM answer"])
    assert fallback.advance_one(str(lane.code), lane.db)
    assert not vm.service.submitted and vm.service.waited == ["local-1"]
    assert collect.get_job(job["job_id"], db_path=lane.db)["answer"] == "late VM answer"


def test_vm_fallback_respects_the_local_admission_budget(lane, vm, monkeypatch):
    real_admission = fallback.admission
    job = _overdue(lane)
    monkeypatch.setattr(fallback, "admission", lambda *args: (None, "prompt needs ~9000 tokens"))
    vm.service = FakeService([])
    assert fallback.advance_one(str(lane.code), lane.db)
    stored = collect.get_job(job["job_id"], db_path=lane.db)
    assert stored["check_status"] == "error" and "unaffordable" in stored["error"]
    assert not vm.service.submitted
    assert real_admission(None, "short", 64)[0] is not None
    assert real_admission(None, "x" * 10_000_000, 64)[0] is None


def test_vm_fallback_waits_for_an_idle_server(lane, vm, monkeypatch):
    job = _overdue(lane)
    monkeypatch.setattr(fallback.lsr, "server_state", lambda *args: (False, "busy"))
    vm.service = FakeService([])
    assert not fallback.advance_one(str(lane.code), lane.db)
    assert collect.get_job(job["job_id"], db_path=lane.db)["checked_at"] is None


def test_interactive_kinds_never_fall_back(lane, vm):
    job = submit(lane, "benchmarks/review.json", "review_draft", deadline=1)
    collect.advance_deadlines(lane.db, now=job["deadline"])
    vm.service = FakeService([])
    assert not fallback.advance_one(str(lane.code), lane.db)
    assert collect.get_job(job["job_id"], db_path=lane.db)["status"] == "expired"
    assert not vm.service.submitted


@pytest.mark.parametrize("path,kind,answer,expected", [
    ("benchmarks/review.json", "review_draft",
     '{"verdict": "approve", "summary": "Looks fine."}', "passed"),
    ("benchmarks/review.json", "review_draft", "I like it", "failed"),
    ("benchmarks/draft.json", "code_draft",
     "pkg/util.py\n<<<<<<< SEARCH\ndef helper():\n=======\ndef renamed():\n>>>>>>> REPLACE\n", "passed"),
    ("benchmarks/draft.json", "code_draft",
     "pkg/util.py\n<<<<<<< SEARCH\ndef missing():\n=======\ndef renamed():\n>>>>>>> REPLACE\n", "failed"),
    ("benchmarks/draft.json", "code_draft",
     "other.py\n<<<<<<< SEARCH\n=======\nimport os\n>>>>>>> REPLACE\n", "failed"),
])
def test_each_generic_kind_has_its_checker(lane, path, kind, answer, expected):
    job = submit(lane, path, kind)
    with closing(init_db(lane.db)) as conn, conn:
        conn.execute("UPDATE home_gpu_jobs SET status='answered', answer=?, answered_at=1 "
                     "WHERE job_id=?", (answer, job["job_id"]))
    assert collect.run_checks(lane.code, lane.db) == 1
    assert collect.get_job(job["job_id"], db_path=lane.db)["check_status"] == expected
    assert collect.run_checks(lane.code, lane.db) == 0


def _replay_job(lane, name, *, key):
    from modules.home_gpu.replay import replay_key

    unit = SimpleNamespace(file=f"pkg/{name}.py", symbol="f")
    job_id = jobs.enqueue("hunk_replay", {"prompt": f"Change {name}."}, {}, 60, db_path=lane.db,
                          job_id=(name * 32)[:32],
                          replay_key=replay_key(key, "home-gpu-v", unit, seed=1, model=None,
                                                kind="hunk_replay"))
    with closing(init_db(lane.db)) as conn, conn:
        conn.execute("UPDATE home_gpu_jobs SET status='answered', answer='x' WHERE job_id=?", (job_id,))
    return job_id


def test_a_finished_or_failing_replay_job_never_starves_the_queue(lane, monkeypatch):
    from modules import local_shadow_replay_hunks as lsh

    finished = _replay_job(lane, "a", key="exp-done")
    flaky = _replay_job(lane, "b", key="exp-flaky")
    good = _replay_job(lane, "c", key="exp-good")
    with closing(init_db(lane.db)) as conn, conn:
        conn.execute("INSERT INTO local_replay_unit_runs (experiment_id, prompt_variant, status, "
                     "created_at, error) VALUES ('exp-done', 'home-gpu-v', 'error', 'now', 'worktree')")
    calls = []

    def replay(experiment_id, **kwargs):
        calls.append((experiment_id, kwargs["home_job_id"]))
        if experiment_id == "exp-flaky":
            return {"ran_units": 0, "transient_error": "worktree: OSError: disk full"}
        return {"ran_units": 1}

    monkeypatch.setattr(lsh, "replay_hunks", replay)
    monkeypatch.setattr(collect, "night_window", lambda now=None: False)
    assert fallback.advance_one(str(lane.code), lane.db)
    assert calls == [("exp-flaky", flaky), ("exp-good", good)]
    done = collect.get_job(finished, db_path=lane.db)
    assert done["check_status"] == "error" and "replay run error" in done["check_detail"]
    retry = collect.get_job(flaky, db_path=lane.db)
    assert retry["checked_at"] is None and retry["check_attempts"] == 1
    assert "disk full" in retry["check_detail"]
    # Running it again repeats nothing that already finished.
    calls.clear()
    fallback.advance_one(str(lane.code), lane.db, now=retry["next_check_at"] - 1)
    assert ("exp-flaky", flaky) not in calls and ("exp-done", finished) not in calls


def test_transient_replay_failures_give_up_after_a_bounded_number(lane, monkeypatch):
    from modules import local_shadow_replay_hunks as lsh

    job_id = _replay_job(lane, "d", key="exp-down")
    monkeypatch.setattr(lsh, "replay_hunks", lambda *a, **k: {"ran_units": 0, "transient_error": "down"})
    now = collect.get_job(job_id, db_path=lane.db)["created_at"]
    for _ in range(fallback.MAX_CHECK_ATTEMPTS):
        fallback.advance_one(str(lane.code), lane.db, now=now)
        now += 7 * 3600
    final = collect.get_job(job_id, db_path=lane.db)
    assert final["check_status"] == "error" and "gave up" in final["check_detail"]


def test_list_and_http_route_show_metadata_only(lane, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setenv("AGENT_API_KEY", "test-home-gpu-key")
    import api

    job = submit(lane)
    monkeypatch.setattr(api, "API_KEY", "test-home-gpu-key")
    route = next(route for route in api.app.routes if route.path == "/home-gpu/jobs")
    service = SimpleNamespace(db_path=lane.db)
    with pytest.raises(HTTPException) as rejected:
        route.endpoint(x_api_key=None, limit=50, service=service)
    assert rejected.value.status_code == 401
    [listed] = route.endpoint(x_api_key="test-home-gpu-key", limit=50, service=service)["jobs"]
    assert listed["job_id"] == job["job_id"] and listed["status"] == "queued"
    assert not {"payload", "answer"} & listed.keys()
    assert {"answered_at", "check_status", "answer_host"} <= listed.keys()
