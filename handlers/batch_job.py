"""Portable mailbox-client handler for a dedicated local llama-server."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
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


def llama_request(kind: str, payload: dict) -> dict:
    if kind not in KINDS or not isinstance(payload, dict):
        raise ValueError("invalid batch request")
    options = {"grammar", "json_schema", "seed", "max_tokens"}
    fields = {"messages"} if kind == "chat_completion" else {"prompt", "expected_diff_refs"}
    if payload.keys() - options - fields:
        raise ValueError("unsupported batch request fields")
    request = {key: payload[key] for key in options if key in payload}
    for key in ("seed", "max_tokens"):
        if key in request and type(request[key]) is not int:
            raise ValueError("invalid token limit or seed")
    if request.get("max_tokens", 1024) < 1:
        raise ValueError("invalid token limit")
    if "grammar" in request and not isinstance(request["grammar"], str):
        raise ValueError("invalid grammar")
    if "json_schema" in request and not isinstance(request["json_schema"], dict):
        raise ValueError("invalid JSON schema")
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


def handle(meta: dict[str, str], body: str, outbox: Path, *, port: int = 8080) -> Path:
    """Handle one parsed message. The mailbox client owns routing and Git sync."""
    job_id = meta.get("job_id", "")
    if (not JOB_ID.fullmatch(job_id) or meta.get("type") != "batch_job"
            or meta.get("to") != "home-gpu" or meta.get("created_by") != "nexus"):
        raise ValueError("invalid batch job envelope")
    raw = body.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_PAYLOAD or digest != meta.get("input_sha256"):
        raise ValueError("invalid batch job input")
    envelope = json.loads(body)
    request = llama_request(meta.get("kind", ""), envelope["request"])
    if outbox.is_symlink():
        raise ValueError("invalid outbox directory")
    path = outbox / f"{job_id}.reply.md"
    if path.is_file() and not path.is_symlink():
        return path
    deadline = float(meta["deadline"])
    remaining = deadline - time.time()
    if not math.isfinite(remaining) or remaining <= 0 or not 1 <= port <= 65535:
        raise ValueError("expired batch job or invalid port")
    started = time.monotonic()
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    http_request = Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(request).encode(), headers={"Content-Type": "application/json"},
    )
    with opener.open(http_request, timeout=min(remaining, 1800)) as response:
        data = json.loads(response.read(4 * MAX_PAYLOAD))
    seconds = time.monotonic() - started
    answer = data["choices"][0]["message"]["content"]
    model = data["model"]
    usage = data["usage"]
    if not isinstance(answer, str) or not isinstance(model, str) or not model.strip():
        raise ValueError("invalid llama-server response")
    if any(not 32 <= ord(char) < 127 for char in model) or len(model) > 200:
        raise ValueError("invalid model identifier")
    prompt_tokens, completion_tokens = usage["prompt_tokens"], usage["completion_tokens"]
    if any(type(tokens) is not int or not 0 <= tokens <= 1000000
           for tokens in (prompt_tokens, completion_tokens)):
        raise ValueError("invalid token counts")
    rate = (data.get("timings") or {}).get("predicted_per_second", completion_tokens / seconds)
    if not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0:
        raise ValueError("invalid generation rate")
    message = (
        f"---\nin_reply_to: {job_id}\ninput_sha256: {digest}\nmodel: {model}\n"
        f"prompt_tokens: {prompt_tokens}\ncompletion_tokens: {completion_tokens}\n"
        f"seconds: {seconds:.6f}\ntokens_per_second: {rate:.6f}\n---\n{answer}\n"
    )
    if len(message.encode("utf-8")) > MAX_PAYLOAD:
        raise ValueError("batch reply too large")
    outbox.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=outbox, delete=False) as tmp:
        temp_path = Path(tmp.name)
        try:
            tmp.write(message.encode("utf-8"))
            tmp.close()
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)
    return path
