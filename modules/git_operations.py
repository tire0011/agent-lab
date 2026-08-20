"""Git wrapper for the operations API.

Only operations within the configured repo root (``/agent`` by default) are
allowed.  All output is redacted before being returned so repository URLs,
absolute paths and any accidental credential exposure are stripped.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from modules.redaction import redact

_logger = logging.getLogger(__name__)

# Only allow git operations inside this root to prevent path-traversal exploits.
_ALLOWED_REPO_ROOT: str = os.environ.get("AGENT_FS_ROOT", "/agent")

# Allowlist of service names that can appear in commit messages/file paths.
_SAFE_FILE_PATTERN = re.compile(r"^[\w./\- ]+$")
_DEPENDENCY_SYNC_TIMEOUT = 300


def _validate_repo_path(repo_path: str) -> Path:
    resolved = Path(repo_path).resolve()
    allowed = Path(_ALLOWED_REPO_ROOT).resolve()
    if resolved != allowed and not str(resolved).startswith(str(allowed) + "/"):
        raise ValueError(f"repo_path must be inside {_ALLOWED_REPO_ROOT}")
    return resolved


async def _run(args: list[str], cwd: str, timeout: int = 30) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, "", "operation timed out"
    return proc.returncode or 0, stdout_b.decode(errors="replace"), stderr_b.decode(errors="replace")


class GitOperations:
    """Thin async wrapper around common git commands."""

    def __init__(self, repo_path: str = _ALLOWED_REPO_ROOT) -> None:
        self._repo_path = str(_validate_repo_path(repo_path))

    async def pull(self) -> dict:
        """Run ``git pull``, then synchronize API dependencies on success."""
        rc, stdout, stderr = await _run(["git", "pull", "--ff-only"], self._repo_path)
        output = redact(stdout + stderr)
        if rc != 0:
            return {
                "success": False,
                "commits_pulled": 0,
                "message": output.strip()[:500],
            }

        sync_script = str(Path(self._repo_path) / "scripts" / "sync-api-deps.sh")
        sync_rc, sync_stdout, sync_stderr = await _run(
            ["/bin/bash", sync_script],
            self._repo_path,
            timeout=_DEPENDENCY_SYNC_TIMEOUT,
        )
        if sync_rc != 0:
            sync_output = redact(sync_stdout + sync_stderr).strip()[:400]
            return {
                "success": False,
                "commits_pulled": 0,
                "message": f"dependency synchronization failed: {sync_output}"[:500],
            }

        # Count "fast-forward" lines as a proxy for commits pulled.
        commits_pulled = stdout.count("\n") - 1 if "Fast-forward" in stdout else 0
        commits_pulled = max(0, commits_pulled)
        return {
            "success": True,
            "commits_pulled": commits_pulled,
            "message": output.strip()[:500],
        }

    async def status(self) -> dict:
        """Return a sanitized git status summary."""
        rc, stdout, _ = await _run(["git", "status", "--porcelain", "-b"], self._repo_path)
        if rc != 0:
            return {"status": "error", "branch": "unknown", "uncommitted": []}
        lines = stdout.splitlines()
        branch = "unknown"
        if lines and lines[0].startswith("##"):
            branch_line = lines[0][3:]
            # Strip remote tracking info like "main...origin/main"
            branch = branch_line.split("...")[0].strip()
        uncommitted = [l[3:] for l in lines[1:] if l.strip()]
        dirty = len(uncommitted) > 0
        return {
            "status": "dirty" if dirty else "clean",
            "branch": branch,
            "uncommitted": uncommitted[:50],  # cap list length
        }

    async def commit_and_push(self, message: str, files: list[str]) -> dict:
        """Stage *files*, commit with *message*, and push."""
        # Validate inputs
        if not message or len(message) > 500:
            raise ValueError("commit message must be 1-500 characters")
        if not files:
            raise ValueError("files list must not be empty")

        repo_root = Path(self._repo_path).resolve()
        validated_files: list[str] = []
        for f in files:
            if not _SAFE_FILE_PATTERN.match(f):
                raise ValueError(f"unsafe file path: {f!r}")
            # Resolve against repo root to block traversal and symlink escapes.
            candidate = (repo_root / f).resolve()
            if candidate != repo_root and not str(candidate).startswith(str(repo_root) + "/"):
                raise ValueError(f"file path escapes repo root: {f!r}")
            # Use the validated relative form for git add.
            relative = str(candidate.relative_to(repo_root))
            if not _SAFE_FILE_PATTERN.match(relative):
                raise ValueError(f"resolved path contains unsafe characters: {relative!r}")
            validated_files.append(relative)

        # Stage files
        rc, _, stderr = await _run(["git", "add", "--"] + validated_files, self._repo_path)
        if rc != 0:
            return {"success": False, "commit_sha": None, "message": redact(stderr)[:300]}

        # Commit
        rc, _, stderr = await _run(["git", "commit", "-m", message], self._repo_path)
        if rc != 0:
            return {"success": False, "commit_sha": None, "message": redact(stderr)[:300]}

        # Get sha
        _, sha, _ = await _run(["git", "rev-parse", "HEAD"], self._repo_path)
        sha = sha.strip()[:40]

        # Push
        rc, _, stderr = await _run(["git", "push"], self._repo_path, timeout=60)
        if rc != 0:
            return {"success": False, "commit_sha": sha, "message": redact(stderr)[:300]}

        return {"success": True, "commit_sha": sha, "message": "pushed successfully"}
