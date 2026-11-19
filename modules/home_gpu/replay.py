"""Transport for hunk replays, with the existing VM scorer as the judge (#743)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from modules.home_gpu import collect
from modules.home_gpu.collect import get_job
from modules.home_gpu.jobs import _git, enqueue, public_blob, public_commit

REPLAY_DEADLINE_SECONDS = 6 * 3600


def replay_key(experiment_id: str, variant: str, unit, *, seed: int, model: str | None,
               kind: str) -> str:
    return json.dumps([experiment_id, variant, seed, model, kind, unit.file, unit.symbol])


def parse_replay_key(raw: str) -> dict:
    values = json.loads(raw)
    names = ("experiment_id", "variant", "seed", "model", "kind", "file", "symbol")
    return dict(zip(names, values + [None] * (len(names) - len(values))))


def replay_job_id(experiment_id: str, variant: str, unit, old: str, new: str, *, seed: int,
                  model: str | None, kind: str) -> str:
    key = json.dumps([experiment_id, variant, unit.file, unit.symbol, old, new, seed, model, kind])
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def replay_job(repo: str, db_path: str, experiment_id: str, variant: str, unit,
               old: str, new: str, *, seed: int, model: str | None, kind: str,
               resume_only: bool = False) -> dict | None:
    """The stored job for this unit, queuing it first unless *resume_only*."""
    from modules.local_coding_worker import _SR_CLOSING
    from modules.local_shadow_replay_hunks import micro_task, worker_start_text

    job_id = replay_job_id(experiment_id, variant, unit, old, new, seed=seed, model=model, kind=kind)
    job = get_job(job_id, db_path=db_path)
    if job or resume_only:
        return job
    root = Path(repo)
    public_commit(root, old)
    public_commit(root, new)
    # The replay reconstructs the unit from these Git objects; they must match.
    merged_text = public_blob(root, new, unit.file, max_bytes=len(unit.merged_text.encode()))
    old_text = (public_blob(root, old, unit.file, max_bytes=len(unit.old_text.encode()))
                if _git(root, "ls-tree", old, "--", unit.file) else "")
    if (old_text, merged_text) != (unit.old_text, unit.merged_text):
        raise ValueError("replay source does not match public Git objects")
    subject = _git(root, "show", "-s", "--format=%s", new).decode().strip()
    start, reduced = worker_start_text(unit)
    task = micro_task(subject, subject, unit, reduced=reduced)
    prompt = f"{task}\n\nCurrent contents of {unit.file}:\n\n{start}\n\n{_SR_CLOSING}"
    enqueue(kind, {"prompt": prompt, "seed": seed, "max_tokens": 4096,
                   **({"expected_diff_refs": [old, new]} if kind == "hunk_replay" else {})},
            {"commit": new, "base_commit": old, "path": unit.file}, REPLAY_DEADLINE_SECONDS,
            db_path=db_path, job_id=job_id, expected_model=model,
            replay_key=replay_key(experiment_id, variant, unit, seed=seed, model=model, kind=kind))
    return get_job(job_id, db_path=db_path)


class ReplyWorker:
    """Use the VM SEARCH/REPLACE converter and patch checks for a remote answer."""

    def __init__(self, job: dict, path: str) -> None:
        self.job = job
        self.path = path

    def run(self, *, working_dir, timeout_seconds, **kwargs):
        from modules.local_coding_worker import apply_and_check, edits_to_diff

        allowed = {self.path}
        patch = edits_to_diff(self.job["answer"], working_dir, timeout_seconds, allowed)
        apply_and_check(patch, working_dir, timeout_seconds, allowed_paths=allowed)
        return SimpleNamespace(success=True, usage={"prompt_tokens": self.job["prompt_tokens"]})


def ready(job: dict, now: float | None = None) -> bool:
    """Whether the replay may score this job now (VM fallbacks only at night)."""
    if job.get("checked_at") is not None:
        return False
    return job["status"] in {"answered", "expired"} or (
        job["status"] == "fallback" and job.get("answer") is None and collect.night_window(now)
    )


def check_status(outcome) -> tuple[str, str | None]:
    """The job's check result from the scorer's unit outcome."""
    if outcome.tests_passed:
        return "passed", None
    return ("failed" if outcome.status == "scored" else "error"), outcome.error
