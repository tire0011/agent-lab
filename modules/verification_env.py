"""Create a disposable environment for verification subprocesses."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

# Keep executable discovery and locale support while dropping service settings,
# proxies, Python startup overrides and credential-store configuration.
_RUNTIME_ENV = {"PATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "WINDIR"}

# The fixed, test-only value the whole test suite already expects (see
# ``os.environ.setdefault("AGENT_API_KEY", "test-key")`` throughout
# tests/*.py). Verification subprocesses must always get exactly this
# value, never whatever real key the parent ``agent-api`` process holds.
VERIFICATION_TEST_API_KEY = "test-key"

# The clock-drift fixture (#687) switch. Only the timing check sets it, for
# its own pytest child; a verification run must never inherit it from its
# parent - e.g. a verification nested in a test that itself runs under the
# timing check (tests/test_issue_689_*) would otherwise run its "normal" test
# run under drift as well (#736).
CLOCK_DRIFT_ENV = "NEXUS_TEST_CLOCK_DRIFT_MS"

# Same value tests/conftest.py assigns: port 9 (discard) refuses at once.
UNREACHABLE_LOCAL_MODEL_URL = "http://127.0.0.1:9"
LOCAL_MODEL_URL_ENVS = ("LOCAL_MODEL_URL", "LLAMA_CPP_BASE_URL", "LLAMA_API_URL")


# Test runs leave temporary files behind (fixtures without cleanup, crashed
# runs). On the VM /tmp is only cleared at reboot, and 2026-11-22 it held 30,000
# leftover directories (7.3 GB, disk 93 % full). Each verification run gets its
# own TMPDIR below this parent; the tick prunes old run directories.
TEST_TMP_PARENT_NAME = "nexus-test-tmp"
TEST_TMP_MAX_AGE_SECONDS = 6 * 3600  # longer than any run's safety cap


def verification_tmp_parent() -> Path:
    return Path(tempfile.gettempdir()) / TEST_TMP_PARENT_NAME


def new_test_tmp_dir() -> Path:
    parent = verification_tmp_parent()
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=parent))


def prune_test_tmp(max_age_seconds: float = TEST_TMP_MAX_AGE_SECONDS,
                   now: float | None = None) -> int:
    """Remove run directories not modified for *max_age_seconds*; returns how
    many were removed. Best effort: a directory that cannot be removed stays."""
    parent = verification_tmp_parent()
    if not parent.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for child in parent.iterdir():
        try:
            if now - child.stat().st_mtime < max_age_seconds:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
            removed += not child.exists()
        except OSError:
            continue
    return removed


def build_verification_test_env(
    base_env: Mapping[str, str] | None = None,
    *,
    run_tmp: Path | None = None,
) -> dict[str, str]:
    """Build a child environment rooted in a disposable run directory."""
    source = os.environ if base_env is None else base_env
    env = {name: source[name] for name in _RUNTIME_ENV if name in source}
    run_tmp = new_test_tmp_dir() if run_tmp is None else run_tmp.resolve()
    home = run_tmp / "home"
    home.mkdir(exist_ok=True)
    env.update({
        "AGENT_API_KEY": VERIFICATION_TEST_API_KEY,
        "AGENT_DB_PATH": str(run_tmp / "agent.db"),
        "AGENT_LOG_DIR": str(run_tmp / "logs"),
        "AGENT_MAILBOX_REPO_PATH": str(run_tmp / "mailbox-repo"),
        "AGENT_CODE_REPO_PATH": str(run_tmp / "code-repo"),
        "AGENT_CODEX_BIN": str(run_tmp / "no-codex"),
        "AGENT_CLAUDE_BIN": str(run_tmp / "no-claude"),
        "AGENT_VIBE_BIN": str(run_tmp / "no-vibe"),
        "LOCAL_MODEL_RATES_PATH": str(run_tmp / "local-model-rates.json"),
        "LOCAL_MODEL_STREAM": "0",
        "NEXUS_DB_MIGRATE_LEGACY": "0",
        # Disposable databases can skip fsync on every commit.
        "NEXUS_DB_DURABILITY": "relaxed",
        "NEXUS_LOG_DIR": str(run_tmp / "structured-logs"),
        "NEXUS_TEST_LOG_DIR": str(run_tmp / "test-runs"),
        "NEXUS_TIMING_CHECK": "0",
        "NEXUS_LLAMA_SERVER_DIAGNOSTICS": "0",
        "NEXUS_HEALTH_CHECK": "0",
        "NEXUS_CLI_UPDATE": "0",
        "NEXUS_WEEKLY_REVIEW": "0",
        "NEXUS_LOG_RETENTION": "0",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(home),
        "USERPROFILE": str(home),
        **dict.fromkeys(("TMPDIR", "TEMP", "TMP"), str(run_tmp)),
        **dict.fromkeys(LOCAL_MODEL_URL_ENVS, UNREACHABLE_LOCAL_MODEL_URL),
    })
    return env


@contextmanager
def verification_test_env(
    base_env: Mapping[str, str] | None = None,
    *,
    clock_drift: str | None = None,
) -> Iterator[dict[str, str]]:
    """Own the run directory until the subprocess and its readers finish."""
    run_tmp = new_test_tmp_dir()
    try:
        env = build_verification_test_env(base_env, run_tmp=run_tmp)
        if clock_drift is not None:
            env[CLOCK_DRIFT_ENV] = clock_drift
        yield env
    finally:
        shutil.rmtree(run_tmp, ignore_errors=True)
