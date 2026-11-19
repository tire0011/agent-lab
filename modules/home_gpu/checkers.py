"""The checker for each generic home GPU job kind (#743).

The PC produces text; it never decides anything. Each kind goes to the
validation path the VM uses for the same kind of output, and none of them
runs code from the answer:

- ``chat_completion``: with a ``json_schema``, the answer must be JSON that
  validates against it (what the VM's constrained decoding guarantees);
  otherwise a non-empty answer;
- ``review_draft``: the reviewer's own parser (``nexus_reviewer.parse_review``);
- ``code_draft``: the SEARCH/REPLACE applier (``local_edit_format.edits_to_diff``)
  against the public files at the job's commit, in a throwaway directory.

Hunk and bake-off jobs are checked by the hunk replay (``modules.home_gpu.replay``).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from modules.home_gpu.jobs import public_blob, safe_repo_path

PASSED, FAILED = "passed", "failed"


def _chat(request: dict, answer: str, **_) -> tuple[str, str | None]:
    schema = request.get("json_schema")
    if schema is None:
        return (PASSED, None) if answer.strip() else (FAILED, "empty answer")
    import jsonschema

    try:
        jsonschema.validate(json.loads(answer), schema)
    except json.JSONDecodeError:
        return FAILED, "answer is not JSON"
    except jsonschema.ValidationError as exc:
        return FAILED, f"answer does not match the schema: {exc.message[:200]}"
    except jsonschema.SchemaError:
        return FAILED, "the job's JSON schema is invalid"
    return PASSED, None


def _review(request: dict, answer: str, **_) -> tuple[str, str | None]:
    from modules.nexus_reviewer import parse_review

    parsed = parse_review(answer)
    return (PASSED, parsed[0]) if parsed else (FAILED, "not a reviewer verdict")


def _code(request: dict, answer: str, *, source: dict, repo: Path) -> tuple[str, str | None]:
    from modules.local_edit_format import EditFormatError, edits_to_diff, parse_edit_blocks

    commit = source["commit"]
    files = [safe_repo_path(name) for name in request.get("files", [])]
    try:
        blocks = parse_edit_blocks(answer)
    except EditFormatError as exc:
        return FAILED, str(exc)[:300]
    allowed = set(files) if files else None
    with tempfile.TemporaryDirectory(prefix="nexus-home-gpu-draft-") as tmp:
        root = Path(tmp)
        for name in {block.path for block in blocks} | set(files):
            try:
                text = public_blob(repo, commit, safe_repo_path(name))
            except ValueError:
                continue  # a new file: the applier treats it as a create
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        try:
            diff = edits_to_diff(answer, tmp, 30, allowed)
        except (EditFormatError, ValueError) as exc:
            return FAILED, str(exc)[:300]
    return (PASSED, f"{len(blocks)} edit block(s) apply") if diff.strip() else (FAILED, "no change")


_CHECKERS = {"chat_completion": _chat, "review_draft": _review, "code_draft": _code}


def check_answer(job: dict, repo: Path) -> tuple[str, str | None]:
    """``(check_status, detail)`` for one answered generic job."""
    checker = _CHECKERS.get(job["kind"])
    if checker is None:
        return "error", "no checker for this kind"
    envelope = json.loads(job["payload"])
    return checker(envelope["request"], job["answer"], source=envelope["source"], repo=repo)
