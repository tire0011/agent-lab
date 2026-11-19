"""Transport for hunk replays, with the existing VM scorer as the judge."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from modules.home_gpu.collect import get_job, night_window
from modules.home_gpu.jobs import _enqueue, _git, public_blob, public_commit
from modules.mailbox.config import MailboxConfig


def replay_job(repo: str, db_path: str, experiment_id: str, variant: str, unit,
               old: str, new: str, *, seed: int, model: str | None, kind: str,
               resume_only: bool = False) -> dict | None:
    from modules.local_coding_worker import _SR_CLOSING
    from modules.local_shadow_replay_hunks import micro_task, worker_start_text

    key = json.dumps([experiment_id, variant, unit.file, unit.symbol, old, new, seed, model, kind])
    job_id = hashlib.sha256(key.encode()).hexdigest()[:32]
    job = get_job(job_id, db_path=db_path)
    if job or resume_only:
        return job
    root = Path(repo)
    public_commit(root, old)
    public_commit(root, new)
    # Validate both the immutable file provenance and regular-file type before
    # using the replay's deterministic reconstruction from these Git objects.
    merged_text = public_blob(root, new, unit.file, max_bytes=len(unit.merged_text.encode()))
    old_text = (public_blob(root, old, unit.file, max_bytes=len(unit.old_text.encode()))
                if _git(root, "ls-tree", old, "--", unit.file) else "")
    if (old_text, merged_text) != (unit.old_text, unit.merged_text):
        raise ValueError("replay source does not match public Git objects")
    subject = _git(root, "show", "-s", "--format=%s", new).decode().strip()
    start, reduced = worker_start_text(unit)
    task = micro_task(subject, subject, unit, reduced=reduced)
    prompt = f"{task}\n\nCurrent contents of {unit.file}:\n\n{start}\n\n{_SR_CLOSING}"
    config = MailboxConfig.from_env()
    config = MailboxConfig.from_paths(root, config.mailbox_repo_path)
    _enqueue(kind, {"prompt": prompt, "seed": seed, "max_tokens": 4096,
                    **({"expected_diff_refs": [old, new]} if kind == "hunk_replay" else {})},
             {"commit": new, "base_commit": old, "path": unit.file}, 21600,
             config=config, db_path=db_path, job_id=job_id, expected_model=model,
             replay_key=json.dumps([experiment_id, variant, seed, model, kind]))
    return {"status": "queued"}


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


def ready(job: dict) -> bool:
    return job["status"] in {"answered", "expired"} or (
        job["status"] == "fallback" and night_window()
    )
