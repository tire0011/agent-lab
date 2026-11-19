"""Run the tests for reply-derived code in an isolated checker (#743).

A home GPU answer is code that crossed a trust boundary, so the merged tests
that score it run inside ``bwrap``: no network, a private ``/tmp``, the
system and Python installation read-only, and only the throwaway replay
worktree and the report directory writable. The Nexus database, the
mailbox, key files and home directories are simply not mounted, and the
environment is the scrubbed verification environment.

Without a working ``bwrap`` the checker is unavailable and the job stays
queued for scoring - it is never scored without isolation.
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT_LINKS = ("/bin", "/sbin", "/lib", "/lib64", "/lib32", "/libx32")
_ETC_FILES = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/alternatives",
              "/etc/localtime", "/etc/nsswitch.conf")


def _python_roots() -> list[str]:
    roots = {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}
    roots.add(os.path.dirname(os.path.realpath(sys.executable)))
    return sorted(root for root in roots if root not in ("/", "/usr") and not root.startswith("/usr/"))


def command(cmd: list[str], worktree: str, scratch: str) -> list[str]:
    """*cmd* wrapped in the sandbox; *worktree* and *scratch* are the only writable paths."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("bwrap is not installed")
    args = [bwrap, "--unshare-all", "--die-with-parent", "--new-session",
            "--ro-bind", "/usr", "/usr"]
    for path in _ROOT_LINKS:
        if os.path.islink(path):
            args += ["--symlink", os.readlink(path), path]
        elif os.path.isdir(path):
            args += ["--ro-bind", path, path]
    for path in _ETC_FILES:
        args += ["--ro-bind-try", path, path]
    for root in _python_roots():
        args += ["--ro-bind", root, root]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for path in (os.path.realpath(worktree), os.path.realpath(scratch)):
        args += ["--bind", path, path]
    args += ["--setenv", "HOME", scratch, "--setenv", "TMPDIR", scratch,
             "--chdir", os.path.realpath(worktree), "--", *cmd]
    return args


@functools.lru_cache(maxsize=1)
def _self_test() -> str | None:
    try:
        with tempfile.TemporaryDirectory(prefix="nexus-sandbox-probe-") as probe:
            done = subprocess.run(
                command([sys.executable, "-c", "import os, socket\n"
                         "assert not os.path.exists('/var/lib/nexus')\n"
                         f"assert not os.path.exists({str(Path.home())!r})\n"
                         "try:\n socket.create_connection(('1.1.1.1', 53), timeout=1)\n"
                         "except OSError:\n pass\nelse:\n raise SystemExit(3)"],
                        probe, probe),
                capture_output=True, timeout=30,
            )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None if done.returncode == 0 else f"sandbox self-test exited {done.returncode}"


def unavailable_reason() -> str | None:
    """``None`` when isolated checking works on this host."""
    if not shutil.which("bwrap"):
        return "bwrap is not installed"
    return _self_test()
