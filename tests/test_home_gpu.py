import hashlib
import json
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from handlers import batch_job
from modules.home_gpu import collect, fallback, jobs
from modules.mailbox.config import MailboxConfig
from modules.mailbox.core import parse_front_matter
from modules.run_store import init_db


@pytest.fixture
def lane(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME_GPU", "1")
    monkeypatch.setenv("AGENT_MAILBOX_LOCK_PATH", str(tmp_path / "mailbox.lock"))
    config = MailboxConfig.from_paths(tmp_path / "code", tmp_path / "mailbox")
    request = {"messages": [{"role": "user", "content": "What is Nexus?"}], "max_tokens": 20}
    monkeypatch.setattr(jobs, "public_commit", lambda *args: None)
    monkeypatch.setattr(jobs, "public_blob", lambda *args: json.dumps(request))
    return config, str(tmp_path / "agent.db"), request


def submit(lane, kind="chat_completion", deadline=21600):
    config, db, _ = lane
    job_id = jobs.submit_job(kind, {"commit": "a" * 40, "path": "benchmarks/request.json"},
                             deadline, config=config, db_path=db)
    return job_id, collect.get_job(job_id, db_path=db)


def reply(config, job, *, digest=None, answer="public answer"):
    config.outbox_dir.mkdir(parents=True, exist_ok=True)
    path = config.outbox_dir / f"{job['job_id']}.reply.md"
    path.write_text(
        f"---\nin_reply_to: {job['job_id']}\ninput_sha256: {digest or job['input_sha256']}\n"
        f"model: local-model\nprompt_tokens: 10\ncompletion_tokens: 6\nseconds: 2\n"
        f"tokens_per_second: 5\n---\n{answer}\n",
        encoding="utf-8",
    )
    return path


def test_submission_and_idempotent_collection(lane):
    config, db, request = lane
    job_id, job = submit(lane)
    meta, body = parse_front_matter((config.inbox_dir / f"{job_id}.md").read_text())
    assert meta["kind"] == "chat_completion"
    assert meta["to"] == "home-gpu"
    assert meta["input_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert json.loads(body)["request"] == request
    reply(config, job, digest="wrong")
    collect.collect(config, db, published={job_id}, now=job["created_at"] + 1)
    assert collect.get_job(job_id, db_path=db)["status"] == "queued"
    reply(config, job)
    collect.collect(config, db, now=job["created_at"] + 2)
    reply(config, job, answer="different payload")
    collect.collect(config, db, now=job["created_at"] + 3)
    answered = collect.get_job(job_id, db_path=db)
    assert answered["answer"] == "public answer"
    assert answered["tokens_per_second"] == 5
    assert answered["model"] == "local-model"
    assert "answer" not in jobs.list_jobs(db_path=db)[0]


@pytest.mark.parametrize("text", [
    "https://github.com/tire0011/agent-lab-private",
    "https://github.com/tire0011/agent-lab/../another",
    "https://other.example/repository",
    "file:///tmp/export",
    "Read ../private.txt",
    "/home/company/notes.txt",
    "C:\\company\\notes.txt",
    "token " + "sk-" + "a" * 16,
])
def test_rejects_external_data(lane, text):
    lane[2]["messages"][0]["content"] = text
    with pytest.raises(ValueError):
        submit(lane)
    assert not lane[0].inbox_dir.exists()


def test_rejects_unproven_inline_text_and_oversize(lane):
    config, db, request = lane
    with pytest.raises(ValueError, match="committed"):
        jobs.submit_job("chat_completion", request, config=config, db_path=db)
    request["messages"][0]["content"] = "é" * 33000
    with pytest.raises(ValueError, match="64 KiB"):
        submit(lane)


def test_public_provenance_checks_canonical_remote_and_reachability(monkeypatch):
    calls = []

    def git(repo, *args):
        calls.append(args)
        if args[0] == "config":
            return (jobs.PUBLIC_REMOTE + ".git\n").encode()
        if args[0] == "cat-file":
            return b"commit\n"
        return b""

    monkeypatch.setattr(jobs, "_git", git)
    commit = "abcde" * 8
    jobs.public_commit(Path("."), commit)
    assert ("merge-base", "--is-ancestor", commit, "refs/remotes/origin/main") in calls
    with pytest.raises(ValueError):
        jobs.public_commit(Path("."), "HEAD")
    monkeypatch.setattr(jobs, "_git", lambda *args: b"https://example.org/private")
    with pytest.raises(ValueError, match="public upstream"):
        jobs.public_commit(Path("."), commit)


def test_expiry_and_offline_do_not_score_late_results(lane, monkeypatch):
    config, db, request = lane
    batch_id, batch = submit(lane, deadline=60)
    monkeypatch.setattr(jobs, "public_blob", lambda *args: '{"prompt":"Review this function."}')
    draft_id, draft = submit(lane, "review_draft", deadline=60)
    late = reply(config, batch)
    collect.collect(config, db, now=max(batch["deadline"], draft["deadline"]))
    assert collect.get_job(batch_id, db_path=db)["status"] == "fallback"
    assert collect.get_job(batch_id, db_path=db)["answer"] is None
    late.unlink()
    assert collect.get_job(draft_id, db_path=db)["error"] == "home_gpu_expired"
    monkeypatch.setattr(jobs, "public_blob", lambda *args: json.dumps(request))
    next_id, next_job = submit(lane)
    with closing(init_db(db)) as conn, conn:
        conn.execute("UPDATE home_gpu_jobs SET published_at=? WHERE job_id=?",
                     (next_job["created_at"] - 86401, batch_id))
    collect.collect(config, db, now=next_job["created_at"] + 1)
    assert collect.get_job(next_id, db_path=db)["error"] == "home_gpu_offline"
    reply(config, next_job)
    collect.collect(config, db, now=next_job["created_at"] + 2)
    assert collect.get_job(next_id, db_path=db)["status"] == "fallback"
    last_id, last_job = submit(lane)
    collect.collect(config, db, now=next_job["created_at"] + 3)
    assert collect.get_job(last_id, db_path=db)["status"] == "queued"


def test_publish_commits_only_the_registered_spool_paths(lane):
    config, db, _ = lane
    job_id, _ = submit(lane)
    name = f"inbox/{job_id}.md"
    recorded = []
    mailbox = SimpleNamespace(
        _git=lambda args, *a, **kw: SimpleNamespace(
            stdout=f"?? {name}\n?? outbox/other.md\n", returncode=int(args[0] == "ls-files"),
        ),
        git_add_and_commit=lambda repo, paths, message: recorded.extend(paths) or True,
    )
    assert collect.publish_pending(config, db, mailbox) == {job_id}
    assert recorded == [config.mailbox_repo_path / name]


def test_worker_uses_loopback_with_no_extra_request_fields(lane, monkeypatch):
    config, db, _ = lane
    job_id, _ = submit(lane)
    meta, body = parse_front_matter((config.inbox_dir / f"{job_id}.md").read_text())
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, _):
            return json.dumps({
                "model": "local-model", "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                "timings": {"predicted_per_second": 42},
            }).encode()

    def post(req, timeout):
        calls.append((req.full_url, json.loads(req.data), timeout))
        return Response()

    monkeypatch.setattr(batch_job, "build_opener", lambda *_: SimpleNamespace(open=post))
    path = batch_job.handle(meta, body, config.outbox_dir, port=9000)
    assert calls[0][0] == "http://127.0.0.1:9000/v1/chat/completions"
    assert calls[0][1]["stream"] is False
    assert path == batch_job.handle(meta, body, config.outbox_dir, port=9000)
    assert len(calls) == 1
    with pytest.raises(ValueError):
        batch_job.handle({**meta, "kind": "shell"}, body, config.outbox_dir)
    collect.collect(config, db)
    assert collect.get_job(job_id, db_path=db)["tokens_per_second"] == 42


def test_vm_completion_fallback_uses_existing_client_at_night(lane, monkeypatch):
    config, db, _ = lane
    job_id, job = submit(lane, deadline=1)
    collect.collect(config, db, now=job["deadline"])
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": "VM answer"}}],
                "usage": {"prompt_tokens": 6, "completion_tokens": 2}}

    client = SimpleNamespace(model="vm-model", chat_completion=complete)
    monkeypatch.setattr(fallback.lsr, "default_replay_client", lambda: client)
    monkeypatch.setattr(fallback.lsr, "server_state", lambda *args: (True, "idle"))
    monkeypatch.setattr(fallback, "night_window", lambda _: False)
    assert not fallback.advance_one(str(config.code_repo_path), db)
    assert not calls
    monkeypatch.setattr(fallback, "night_window", lambda _: True)
    assert fallback.advance_one(str(config.code_repo_path), db)
    result = collect.get_job(job_id, db_path=db)
    assert result["status"] == "fallback"
    assert result["answer"] == "VM answer"
    assert result["model"] == "vm-model"
    assert not fallback.advance_one(str(config.code_repo_path), db)


def test_http_jobs_route_registers_an_authenticated_metadata_reader(lane, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setenv("AGENT_API_KEY", "test-home-gpu-key")
    import api

    _, db, _ = lane
    job_id, _ = submit(lane)
    monkeypatch.setattr(api, "API_KEY", "test-home-gpu-key")
    route = next(route for route in api.app.routes if route.path == "/home-gpu/jobs")
    assert "GET" in route.methods
    service = SimpleNamespace(db_path=db)
    with pytest.raises(HTTPException) as rejected:
        route.endpoint(x_api_key=None, limit=50, service=service)
    assert rejected.value.status_code == 401
    [job] = route.endpoint(x_api_key="test-home-gpu-key", limit=50, service=service)["jobs"]
    assert job["job_id"] == job_id
    assert job["status"] == "queued"
    assert not {"payload", "answer"} & job.keys()
