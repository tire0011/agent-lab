"""Home GPU batch-job protocol and the PC-side ``batch_job`` handler (#743).

Stdlib only, so the PC can run it from a plain agent-lab checkout. Nexus and
the PC share this module, which keeps the wire format, the signatures and the
request whitelist identical on both ends.

Trust model: the mailbox repository is a transport, not an authority. Front
matter such as ``created_by: nexus`` can be written by anybody who can push
there, and a SHA-256 of the body can be recomputed by anybody. Each job
therefore carries an HMAC-SHA256 signature made with a key that only Nexus
and the PC hold, and each reply carries one made with the same key under a
different domain label, so a reply can never be passed off as a job or the
other way round. A message without a valid signature is refused.

The handler only sends one HTTP request to the loopback llama-server. It runs
no code from the payload and starts no process.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import time
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_PAYLOAD = 65536
TEMPLATES = {
    "hunk_replay": "Implement the requested change. Return SEARCH/REPLACE blocks with the file path.",
    "bakeoff_prompt": "Answer the benchmark prompt.",
    "review_draft": "Draft a code review of the supplied material.",
    "code_draft": "Draft a code change for the supplied task.",
}
KINDS = frozenset(TEMPLATES) | {"chat_completion"}
JOB_ID = re.compile(r"[0-9a-f]{32}")
ENV_KEY_FILE = "NEXUS_HOME_GPU_KEY_FILE"
MIN_KEY_BYTES = 32
_JOB_DOMAIN = b"agent-lab/home-gpu/job/v1\n"
_REPLY_DOMAIN = b"agent-lab/home-gpu/reply/v1\n"
_SIGNATURE = re.compile(r"hmac-sha256:([0-9a-f]{64})")
_META_LINE = re.compile(r"([a-z_][a-z0-9_]*): ?(.*)")
_SAFE_VALUE = re.compile(r"[\x20-\x7e]{0,200}")
JOB_FIELDS = ("type", "to", "created_by", "job_id", "kind", "deadline", "input_sha256", "signature")
REPLY_FIELDS = ("type", "to", "in_reply_to", "input_sha256", "model", "prompt_tokens",
                "completion_tokens", "seconds", "tokens_per_second", "signature")


class SigningKeyError(ValueError):
    """The shared home GPU key is missing, short or readable by other users."""


def load_key(path: str | os.PathLike[str] | None = None) -> bytes:
    """The shared HMAC key from *path* or ``NEXUS_HOME_GPU_KEY_FILE``.

    The value is never logged or returned in an error message.
    """
    raw_path = path if path is not None else os.environ.get(ENV_KEY_FILE, "")
    if not raw_path:
        raise SigningKeyError(f"{ENV_KEY_FILE} is not set")
    key_path = Path(raw_path)
    try:
        if os.name == "posix" and key_path.stat().st_mode & 0o077:
            raise SigningKeyError("home GPU key file must not be readable by other users")
        key = key_path.read_bytes().strip()
    except OSError as exc:
        raise SigningKeyError("home GPU key file is unreadable") from exc
    if len(key) < MIN_KEY_BYTES:
        raise SigningKeyError(f"home GPU key must have at least {MIN_KEY_BYTES} bytes")
    return key


def _mac(key: bytes, domain: bytes, fields: list[str]) -> str:
    message = domain + "\n".join(fields).encode("utf-8")
    return "hmac-sha256:" + hmac.new(key, message, hashlib.sha256).hexdigest()


def _job_fields(meta: dict[str, str]) -> list[str]:
    return [meta["job_id"], meta["kind"], meta["deadline"], meta["input_sha256"]]


def _reply_fields(meta: dict[str, str], answer: str) -> list[str]:
    return [meta["in_reply_to"], meta["input_sha256"], meta["model"], meta["prompt_tokens"],
            meta["completion_tokens"], meta["seconds"], meta["tokens_per_second"],
            hashlib.sha256(answer.encode("utf-8")).hexdigest()]


def _verify(key: bytes, domain: bytes, fields: list[str], signature: str) -> bool:
    if not _SIGNATURE.fullmatch(signature or ""):
        return False
    return hmac.compare_digest(_mac(key, domain, fields), signature)


def split_message(text: str) -> tuple[dict[str, str], str]:
    """Strict front matter: ``---``, unique ``key: value`` lines, ``---``, body.

    Both ends parse with this function, so the signed values are exactly the
    values that are checked. The body loses exactly one trailing newline.
    """
    if not text.startswith("---\n"):
        raise ValueError("message has no front matter")
    end = text.find("\n---\n", 3)
    if end < 0:
        raise ValueError("front matter is not closed")
    meta: dict[str, str] = {}
    for line in text[4:end].split("\n"):
        match = _META_LINE.fullmatch(line)
        if not match or match.group(1) in meta or not _SAFE_VALUE.fullmatch(match.group(2)):
            raise ValueError("malformed front matter")
        meta[match.group(1)] = match.group(2)
    body = text[end + 5:]
    return meta, body[:-1] if body.endswith("\n") else body


def render_message(meta: dict[str, str], body: str) -> str:
    for key, value in meta.items():
        if not _META_LINE.fullmatch(f"{key}: {value}") or not _SAFE_VALUE.fullmatch(value):
            raise ValueError("unsafe front matter value")
    return "---\n" + "".join(f"{key}: {value}\n" for key, value in meta.items()) + f"---\n{body}\n"


def job_message(key: bytes, *, job_id: str, kind: str, deadline: float, body: str) -> str:
    """The signed inbox message for one job; *body* is the JSON envelope."""
    meta = {"type": "batch_job", "to": "home-gpu", "created_by": "nexus", "job_id": job_id,
            "kind": kind, "deadline": f"{deadline:.3f}",
            "input_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()}
    meta["signature"] = _mac(key, _JOB_DOMAIN, _job_fields(meta))
    return render_message(meta, body)


def verify_job(key: bytes, meta: dict[str, str], body: str) -> None:
    """Raise unless *meta*/*body* is a complete job signed by Nexus."""
    if tuple(meta) != JOB_FIELDS:
        raise ValueError("invalid batch job envelope")
    if (not JOB_ID.fullmatch(meta["job_id"]) or meta["type"] != "batch_job"
            or meta["to"] != "home-gpu" or meta["created_by"] != "nexus"
            or meta["kind"] not in KINDS):
        raise ValueError("invalid batch job envelope")
    raw = body.encode("utf-8")
    if len(raw) > MAX_PAYLOAD or hashlib.sha256(raw).hexdigest() != meta["input_sha256"]:
        raise ValueError("invalid batch job input")
    if not _verify(key, _JOB_DOMAIN, _job_fields(meta), meta["signature"]):
        raise ValueError("batch job signature is not valid")


def reply_message(key: bytes, *, job_id: str, digest: str, model: str, prompt_tokens: int,
                  completion_tokens: int, seconds: float, rate: float, answer: str) -> str:
    meta = {"type": "batch_reply", "to": "nexus", "in_reply_to": job_id, "input_sha256": digest,
            "model": model, "prompt_tokens": str(prompt_tokens),
            "completion_tokens": str(completion_tokens), "seconds": f"{seconds:.6f}",
            "tokens_per_second": f"{rate:.6f}"}
    meta["signature"] = _mac(key, _REPLY_DOMAIN, _reply_fields(meta, answer))
    return render_message(meta, answer)


def verify_reply(key: bytes, meta: dict[str, str], answer: str, *, job_id: str,
                 digest: str) -> dict:
    """The checked reply values, or ``ValueError``. Nothing here is trusted unsigned."""
    if tuple(meta) != REPLY_FIELDS or meta["type"] != "batch_reply" or meta["to"] != "nexus":
        raise ValueError("invalid reply envelope")
    if meta["in_reply_to"] != job_id or meta["input_sha256"] != digest:
        raise ValueError("reply does not match job or input digest")
    if not _verify(key, _REPLY_DOMAIN, _reply_fields(meta, answer), meta["signature"]):
        raise ValueError("reply signature is not valid")
    model = meta["model"]
    if not model.strip():
        raise ValueError("invalid reply model")
    counts = []
    for name in ("prompt_tokens", "completion_tokens"):
        if not re.fullmatch(r"\d{1,7}", meta[name]):
            raise ValueError("invalid reply token counts")
        counts.append(int(meta[name]))
    seconds, rate = float(meta["seconds"]), float(meta["tokens_per_second"])
    if not math.isfinite(seconds) or not 0 < seconds <= 86400:
        raise ValueError("invalid reply duration")
    if not math.isfinite(rate) or rate < 0:
        raise ValueError("invalid reply rate")
    if not answer.strip() or "\x00" in answer:
        raise ValueError("invalid reply content")
    return {"model": model, "prompt_tokens": counts[0], "completion_tokens": counts[1],
            "seconds": seconds, "tokens_per_second": rate, "answer": answer}


def llama_request(kind: str, payload: dict) -> dict:
    """The one llama-server request a job may cause; anything else is refused."""
    if kind not in KINDS or not isinstance(payload, dict):
        raise ValueError("invalid batch request")
    options = {"grammar", "json_schema", "seed", "max_tokens"}
    fields = {"messages"} if kind == "chat_completion" else {"prompt"}
    if kind == "hunk_replay":
        fields = fields | {"expected_diff_refs"}
    if kind == "code_draft":
        fields = fields | {"files"}
    if payload.keys() - options - fields:
        raise ValueError("unsupported batch request fields")
    request = {key: payload[key] for key in options if key in payload}
    for key in ("seed", "max_tokens"):
        if key in request and type(request[key]) is not int:
            raise ValueError("invalid token limit or seed")
    if not 1 <= request.get("max_tokens", 1024) <= 32768:
        raise ValueError("invalid token limit")
    if "grammar" in request and not isinstance(request["grammar"], str):
        raise ValueError("invalid grammar")
    if "json_schema" in request and not isinstance(request["json_schema"], dict):
        raise ValueError("invalid JSON schema")
    if "files" in payload and (not isinstance(payload["files"], list) or not all(
            isinstance(item, str) for item in payload["files"])):
        raise ValueError("invalid file list")
    if kind == "chat_completion":
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages or any(
            not isinstance(msg, dict) or msg.keys() != {"role", "content"}
            or msg["role"] not in ("system", "user", "assistant")
            or not isinstance(msg["content"], str) for msg in messages
        ):
            raise ValueError("invalid messages")
    else:
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("missing prompt")
        messages = [{"role": "system", "content": TEMPLATES[kind]},
                    {"role": "user", "content": prompt}]
    return {**request, "messages": messages, "temperature": 0, "stream": False}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def reply_path(outbox: Path, job_id: str) -> Path:
    return outbox / f"{job_id}.reply.md"


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def handle(meta: dict[str, str], body: str, outbox: Path, *, key: bytes, port: int = 8080,
           now: float | None = None, opener=None) -> Path | None:
    """Answer one job. ``None`` when it expired before it could be started."""
    verify_job(key, meta, body)
    envelope = json.loads(body)
    if not isinstance(envelope, dict) or set(envelope) != {"source", "request"}:
        raise ValueError("invalid batch job body")
    request = llama_request(meta["kind"], envelope["request"])
    if outbox.is_symlink() or not 1 <= port <= 65535:
        raise ValueError("invalid outbox directory or port")
    path = reply_path(outbox, meta["job_id"])
    if path.is_file() and not path.is_symlink():
        return path
    remaining = float(meta["deadline"]) - (time.time() if now is None else now)
    if not math.isfinite(remaining) or remaining <= 0:
        return None
    started = time.monotonic()
    opener = opener or build_opener(ProxyHandler({}), _NoRedirect())
    http_request = Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(request).encode(), headers={"Content-Type": "application/json"},
    )
    with opener.open(http_request, timeout=min(remaining, 1800)) as response:
        data = json.loads(response.read(4 * MAX_PAYLOAD))
    seconds = max(time.monotonic() - started, 1e-6)
    answer = data["choices"][0]["message"]["content"]
    model = data["model"]
    usage = data["usage"]
    if not isinstance(answer, str) or not answer.strip() or not isinstance(model, str):
        raise ValueError("invalid llama-server response")
    if not model.strip() or not _SAFE_VALUE.fullmatch(model):
        raise ValueError("invalid model identifier")
    prompt_tokens, completion_tokens = usage["prompt_tokens"], usage["completion_tokens"]
    if any(type(tokens) is not int or not 0 <= tokens <= 1000000
           for tokens in (prompt_tokens, completion_tokens)):
        raise ValueError("invalid token counts")
    rate = (data.get("timings") or {}).get("predicted_per_second", completion_tokens / seconds)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0:
        raise ValueError("invalid generation rate")
    message = reply_message(key, job_id=meta["job_id"], digest=meta["input_sha256"], model=model,
                            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                            seconds=seconds, rate=float(rate), answer=answer)
    if len(message.encode("utf-8")) > MAX_PAYLOAD:
        raise ValueError("batch reply too large")
    outbox.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, message)
    return path
