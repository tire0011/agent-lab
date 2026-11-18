"""Free main-branch test gate before Nexus books a paid coding start (#739)."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from modules.local_file_localizer import parse_likely_files
from modules.timing_check import affected_tests, failure_tails
from modules.verification_env import CLOCK_DRIFT_ENV, build_verification_test_env

MAX_TEST_IDS = 40
_PATH_RE = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*[\w.-]+\.py)(?![\w./-])")
_FAILED_RE = re.compile(r"^FAILED\s+(.+?)(?:\s+-\s+.*)?$", re.MULTILINE)


@dataclass(frozen=True)
class PreflightResult:
    status: str
    test_ids: tuple[str, ...] = ()
    failing_ids: tuple[str, ...] = ()
    tails: dict[str, str] = field(default_factory=dict)
    wall_seconds: float = 0.0
    signature: str = ""
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"


def likely_tests(issue_text: str, repo: str | os.PathLike[str]) -> list[str]:
    """Select named tests and tests importing named/localized Python modules."""
    root = Path(repo)
    paths = list(dict.fromkeys(
        [match.group(1) for match in _PATH_RE.finditer(issue_text or "")]
        + parse_likely_files(issue_text or "")
    ))
    named_tests: list[str] = []
    modules: list[str] = []
    for rel in paths:
        rel = rel.removeprefix("./")
        if not (root / rel).is_file():
            continue
        name = PurePosixPath(rel).name
        if name.startswith("test_") or name.endswith("_test.py"):
            named_tests.append(rel)
        else:
            modules.append(rel)
    synthetic_diff = "\n".join(f"diff --git a/{path} b/{path}" for path in modules)
    linked = affected_tests(synthetic_diff, root) if modules else []
    return list(dict.fromkeys([*named_tests, *linked]))[:MAX_TEST_IDS]


def _short_tails(tails: dict[str, str]) -> dict[str, str]:
    return {key: "\n".join(value.splitlines()[-12:])[-3000:] for key, value in tails.items()}


def _signature(ids: list[str], tails: dict[str, str]) -> str:
    # Paths/ids identify the host failure much more stably than volatile durations.
    material = "\n".join(sorted(ids)) + "\n" + "\n".join(
        re.sub(r"\d+(?:\.\d+)?s", "<time>", tails.get(test_id, "")) for test_id in sorted(ids)
    )
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:20]


def preflight(issue_text: str, repo: str | os.PathLike[str], budget_s: float = 300) -> PreflightResult:
    """Run likely tests on clean ``main``; infrastructure faults never block work.

    Keep the exception boundary outside selection as well as execution.  The
    localizer walks caller-controlled repository contents and must not be able
    to abort the initiative tick before a paid launch.
    """
    started = time.monotonic()
    try:
        return _preflight(issue_text, repo, budget_s, started)
    except Exception as exc:  # noqa: BLE001 - a free advisory gate fails open
        return PreflightResult(
            "skipped", wall_seconds=time.monotonic() - started,
            reason=f"preflight unavailable: {type(exc).__name__}",
        )


def _preflight(
    issue_text: str,
    repo: str | os.PathLike[str],
    budget_s: float,
    started: float,
) -> PreflightResult:
    """Implementation under :func:`preflight`'s fail-open boundary."""
    root = Path(repo).resolve()
    ids = likely_tests(issue_text, root)
    if not ids:
        return PreflightResult("green", wall_seconds=time.monotonic() - started)
    remaining = float(budget_s) - (time.monotonic() - started)
    if remaining <= 0:
        return PreflightResult("skipped", tuple(ids), wall_seconds=time.monotonic() - started,
                               reason="preflight budget exhausted")

    with tempfile.TemporaryDirectory(prefix="nexus-gate-preflight-") as parent:
        worktree = Path(parent) / "main"
        try:
            added = subprocess.run(
                ["git", "worktree", "add", "--detach", str(worktree), "main"],
                cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=max(0.01, remaining), check=False,
            )
            if added.returncode != 0:
                return PreflightResult("skipped", tuple(ids), wall_seconds=time.monotonic() - started,
                                       reason="could not create clean main worktree")
            remaining = float(budget_s) - (time.monotonic() - started)
            if remaining <= 0:
                return PreflightResult("skipped", tuple(ids), wall_seconds=time.monotonic() - started,
                                       reason="preflight budget exhausted")
            report = Path(parent) / "junit.xml"
            env = build_verification_test_env()
            env[CLOCK_DRIFT_ENV] = "0"
            env["PYTHONUNBUFFERED"] = "1"
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "--tb=short", f"--junitxml={report}", *ids],
                cwd=worktree, env=env, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=max(0.01, remaining), check=False,
            )
            if proc.returncode == 0:
                return PreflightResult("green", tuple(ids), wall_seconds=time.monotonic() - started)
            failed = list(dict.fromkeys(_FAILED_RE.findall(proc.stdout or "")))
            if proc.returncode != 1 or not failed:
                return PreflightResult("skipped", tuple(ids), wall_seconds=time.monotonic() - started,
                                       reason="pytest produced no test verdict")
            tails: dict[str, str] = {}
            if report.is_file():
                try:
                    tails = failure_tails(report, failed, worktree)
                except (OSError, ElementTree.ParseError):
                    pass
            tails = _short_tails(tails)
            return PreflightResult(
                "blocked", tuple(ids), tuple(failed), tails,
                time.monotonic() - started, _signature(failed, tails),
            )
        except subprocess.TimeoutExpired:
            return PreflightResult("skipped", tuple(ids), wall_seconds=time.monotonic() - started,
                                   reason="preflight budget exhausted")
        except (OSError, ValueError) as exc:
            return PreflightResult("skipped", tuple(ids), wall_seconds=time.monotonic() - started,
                                   reason=f"preflight unavailable: {type(exc).__name__}")
        finally:
            if worktree.exists():
                try:
                    subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                                   cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=10, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass  # TemporaryDirectory still removes files; a later git prune clears metadata.
