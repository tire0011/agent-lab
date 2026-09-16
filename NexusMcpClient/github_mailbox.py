"""Narrow GitHub Contents API fallback for submitting Nexus inbox messages."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from urllib import error, request

MAX_FILE_SIZE = 65536
SUPPORTED_TYPES = {"idea", "task", "question", "context", "operation_chain"}
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{10,}"),
    re.compile(r"AZURE_[A-Z_]+\s*=\s*\S"),
    re.compile(r"API_KEY\s*=\s*\S"),
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*\S"),
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class GitHubMailboxError(RuntimeError):
    pass


def _validate(msg_type: str, created_by: str, body: str) -> None:
    if msg_type not in SUPPORTED_TYPES:
        raise GitHubMailboxError(f"unsupported mailbox type: {msg_type}")
    if not _SAFE_IDENTIFIER.fullmatch(created_by):
        raise GitHubMailboxError("created_by contains unsupported characters")
    if not body.strip():
        raise GitHubMailboxError("body must not be empty")
    if len(body.encode("utf-8")) > MAX_FILE_SIZE:
        raise GitHubMailboxError("body is too large")
    if any(pattern.search(body) for pattern in _SECRET_PATTERNS):
        raise GitHubMailboxError("potential secret detected in message body")


def _filename(msg_type: str, created_by: str, body: str, request_id: str | None) -> str:
    if request_id:
        if not _SAFE_IDENTIFIER.fullmatch(request_id):
            raise GitHubMailboxError("request_id contains unsupported characters")
        return f"{msg_type}-{request_id}.md"
    digest = hashlib.sha256(f"{msg_type}\0{created_by}\0{body}".encode()).hexdigest()[:16]
    return f"{msg_type}-{digest}.md"


def _content(msg_type: str, created_by: str, body: str, request_id: str | None) -> str:
    lines = ["---", f"type: {msg_type}", f"created_by: {created_by}"]
    if request_id:
        lines.append(f"request_id: {request_id}")
    return "\n".join(lines + ["---", "", body.rstrip(), ""])


def submit_github_message(*, msg_type: str, created_by: str, body: str, request_id: str | None = None) -> dict[str, str]:
    if os.environ.get("MCP_WRITE_ENABLED", "").lower() != "true":
        raise GitHubMailboxError("write tools are disabled")
    repository = os.environ.get("NEXUS_MAILBOX_GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not repository or not token:
        raise GitHubMailboxError("NEXUS_MAILBOX_GITHUB_REPOSITORY and GITHUB_TOKEN are required")
    _validate(msg_type, created_by, body)
    filename = _filename(msg_type, created_by, body, request_id)
    payload = {
        "message": f"Add mailbox message: {msg_type} from {created_by}",
        "content": base64.b64encode(_content(msg_type, created_by, body, request_id).encode()).decode(),
        "branch": os.environ.get("NEXUS_MAILBOX_GITHUB_BRANCH", "main").strip() or "main",
    }
    url = f"https://api.github.com/repos/{repository}/contents/inbox/{filename}"
    req = request.Request(url, data=json.dumps(payload).encode(), headers={
        "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
        "Content-Type": "application/json", "User-Agent": "nexus-mcp-client",
    }, method="PUT")
    try:
        with request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())
    except error.HTTPError as exc:
        raise GitHubMailboxError(f"GitHub Contents API returned HTTP {exc.code}") from exc
    except (error.URLError, TimeoutError) as exc:
        raise GitHubMailboxError("GitHub Contents API connection failed") from exc
    return {"status": "queued", "filename": filename, "repository": repository,
            "branch": payload["branch"], "commit_sha": str((result.get("commit") or {}).get("sha") or "unknown")}
