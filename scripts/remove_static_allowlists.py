from pathlib import Path
import re


def replace(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"target not found in {path}: {old[:80]!r}")
    p.write_text(text.replace(old, new))


def sub(path: str, pattern: str, repl: str, count: int = 0, flags: int = 0) -> None:
    p = Path(path)
    text = p.read_text()
    new, n = re.subn(pattern, repl, text, count=count, flags=flags)
    if n == 0:
        raise SystemExit(f"pattern not found in {path}: {pattern[:100]!r}")
    p.write_text(new)


# Operation chains: registry membership is the only operation boundary.
p = Path("modules/operation_chain_service.py")
t = p.read_text()
t = re.sub(r"\n# Allowlist of operations that can run in chains \(read-only only\)\n_CHAIN_OPERATION_ALLOWLIST = \{.*?\n\}\n", "\n", t, flags=re.S)
t = re.sub(r"\n# Forbidden patterns in arguments \(basic injection prevention\)\n_FORBIDDEN_PATTERNS = \{.*?\n\}\n", "\n", t, flags=re.S)
t = re.sub(r"\n\s*# Action in allowlist\?\n\s*if step\.action not in _CHAIN_OPERATION_ALLOWLIST:\n\s*return False, f\"action '\{step\.action\}' not in allowlist\"\n", "\n", t)
t = re.sub(r"\n\s*# Arguments safe\?\n\s*for key, value in step\.arguments\.items\(\):\n\s*val_str = str\(value\)\n\s*for pattern in _FORBIDDEN_PATTERNS:\n\s*if pattern in val_str:\n\s*return False, f\"forbidden pattern '\{pattern\}' in argument '\{key\}'\"\n", "\n", t)
t = t.replace("Service for executing validated, ordered chains of read-only operations.", "Service for executing validated, ordered operation chains.")
p.write_text(t)

# Service operations: arbitrary service names are accepted.
p = Path("modules/service_operations.py")
t = p.read_text()
t = re.sub(r"Only the explicitly whitelisted service names may be restarted or queried\.\n", "Service names are passed through to systemd without a static application allowlist.\n", t)
t = re.sub(r"\n# Services the agent is allowed to interact with\..*?_ALLOWED_SERVICES: frozenset\[str\] = frozenset\(\{.*?\n\}\)\n", "\n_DEFAULT_NEXUS_SERVICES = (\"agent-api\", \"nexus-mailbox\", \"llama-server\")\n", t, flags=re.S)
t = re.sub(r"\n\ndef _validate_service\(service: str\) -> None:\n.*?\n\n\nclass ServiceOperations", "\n\nclass ServiceOperations", t, flags=re.S)
t = t.replace('    """Async wrapper around ``systemctl`` for allowed services."""', '    """Async wrapper around ``systemctl`` without a static service allowlist."""')
t = t.replace('        """Restart one service or all allowed services."""\n        _validate_service(service)\n        targets = list(_ALLOWED_SERVICES) if service == "all" else [service]', '        """Restart one service. ``all`` remains a convenience for Nexus services."""\n        targets = list(_DEFAULT_NEXUS_SERVICES) if service == "all" else [service]')
t = t.replace('        """Return status of all allowed services."""\n        result: dict[str, str] = {}\n        for svc in sorted(_ALLOWED_SERVICES):', '        """Return status of the default Nexus services."""\n        result: dict[str, str] = {}\n        for svc in _DEFAULT_NEXUS_SERVICES:')
p.write_text(t)

# Log reader: arbitrary service names are accepted.
p = Path("modules/log_reader.py")
t = p.read_text()
t = re.sub(r"explicitly allowed on-disk log files", "on-disk log files", t)
t = re.sub(r"\n_ALLOWED_SERVICES: frozenset\[str\] = frozenset\(\{.*?\n\}\)\n", "\n", t, flags=re.S)
t = re.sub(r"\n\ndef _validate_service\(service: str\) -> None:\n.*?\n\n\nasync def _run_journalctl", "\n\nasync def _run_journalctl", t, flags=re.S)
t = t.replace("        _validate_service(service)\n", "")
p.write_text(t)

# Git operations: arbitrary repository paths and git pathspecs are accepted.
p = Path("modules/git_operations.py")
t = p.read_text()
t = t.replace("import re\n", "")
t = t.replace("Only operations within the configured repo root (``/agent`` by default) are\nallowed.  ", "Repository paths are caller-selected.  ")
t = t.replace('# Only allow git operations inside this root to prevent path-traversal exploits.\n_ALLOWED_REPO_ROOT: str = os.environ.get("AGENT_FS_ROOT", "/agent")\n\n# Allowlist of service names that can appear in commit messages/file paths.\n_SAFE_FILE_PATTERN = re.compile(r"^[\\w./\\- ]+$")\n', '_DEFAULT_REPO_ROOT: str = os.environ.get("AGENT_FS_ROOT", "/agent")\n')
t = re.sub(r"\ndef _validate_repo_path\(repo_path: str\) -> Path:\n.*?\n\n\nasync def _run", "\ndef _resolve_repo_path(repo_path: str) -> Path:\n    resolved = Path(repo_path).resolve()\n    if not resolved.is_dir():\n        raise ValueError(f\"repo_path does not exist or is not a directory: {resolved}\")\n    return resolved\n\n\nasync def _run", t, flags=re.S)
t = t.replace("    def __init__(self, repo_path: str = _ALLOWED_REPO_ROOT) -> None:\n        self._repo_path = str(_validate_repo_path(repo_path))", "    def __init__(self, repo_path: str = _DEFAULT_REPO_ROOT) -> None:\n        self._repo_path = str(_resolve_repo_path(repo_path))")
old = '''        repo_root = Path(self._repo_path).resolve()\n        validated_files: list[str] = []\n        for f in files:\n            if not _SAFE_FILE_PATTERN.match(f):\n                raise ValueError(f"unsafe file path: {f!r}")\n            # Resolve against repo root to block traversal and symlink escapes.\n            candidate = (repo_root / f).resolve()\n            if candidate != repo_root and not str(candidate).startswith(str(repo_root) + "/"):\n                raise ValueError(f"file path escapes repo root: {f!r}")\n            # Use the validated relative form for git add.\n            relative = str(candidate.relative_to(repo_root))\n            if not _SAFE_FILE_PATTERN.match(relative):\n                raise ValueError(f"resolved path contains unsafe characters: {relative!r}")\n            validated_files.append(relative)\n\n        # Stage files\n        rc, _, stderr = await _run(["git", "add", "--"] + validated_files, self._repo_path)\n'''
new = '''        # Git receives pathspecs as argv entries (never through a shell).\n        rc, _, stderr = await _run(["git", "add", "--"] + list(files), self._repo_path)\n'''
if old not in t:
    raise SystemExit("git validation block not found")
t = t.replace(old, new)
p.write_text(t)

# Codex worker: allowed_roots remains only as a compatibility argument and is ignored.
p = Path("modules/codex_worker.py")
t = p.read_text()
t = t.replace('_DEFAULT_ALLOWED_ROOTS: tuple[str, ...] = ("/agent",)\n', '')
t = t.replace('  - Working directory must be absolute, exist, and be inside allowed_roots.\n', '  - Working directory must be absolute and exist.\n')
t = t.replace('        allowed_roots: tuple[str, ...] = _DEFAULT_ALLOWED_ROOTS,', '        allowed_roots: tuple[str, ...] | None = None,')
t = t.replace('        self.allowed_roots = tuple(str(r).rstrip("/") for r in allowed_roots)\n', '        # Compatibility only: working directories are no longer restricted by an application allowlist.\n        self.allowed_roots = tuple(allowed_roots or ())\n')
t = re.sub(r"\n\s*if not any\(\n\s*resolved == root or resolved\.startswith\(root \+ \"/\"\)\n\s*for root in self\.allowed_roots\n\s*\):\n\s*raise WorkerConfigError\(\n\s*f\"working_dir \{resolved!r\} is not inside allowed roots: \{self\.allowed_roots\}\"\n\s*\)\n", "\n", t)
t = t.replace('            Absolute path to the working directory. Must exist and be inside\n            one of ``allowed_roots``.\n', '            Absolute path to the working directory. Must exist.\n')
t = t.replace('    allowed_roots:\n        Absolute directory paths the working_dir must be inside.\n        Defaults to ``("/agent",)``.\n', '    allowed_roots:\n        Deprecated compatibility argument; no longer restricts working directories.\n')
p.write_text(t)

# Verification environment: no passthrough allowlist; inherit environment minus secrets.
p = Path("modules/verification_env.py")
t = p.read_text()
t = t.replace("from typing import Iterator, Mapping\n", "from typing import Iterator, Mapping\nimport re\n")
t = re.sub(r"\n# Runtime plumbing only:.*?SAFE_RUNTIME_PASSTHROUGH: frozenset\[str\] = frozenset\(\{.*?\n\}\)\n", "\n_SECRET_ENV_RE = re.compile(r\"(?i)(key|secret|token|password|credential|authorization)\")\n", t, flags=re.S)
t = t.replace('    Starts from an *empty* environment (never a copy of the full parent\n    environment), copies over only the small safe-runtime allowlist above\n    from *base_env* (``os.environ`` by default), and then explicitly sets\n    ``AGENT_API_KEY`` to the fixed test value. As a result, no production\n', '    Starts from the parent environment, removes secret-shaped variables,\n    and then explicitly sets ``AGENT_API_KEY`` to the fixed test value. No static\n    environment allowlist is used. As a result, no production\n')
t = t.replace('    env = {key: source[key] for key in SAFE_RUNTIME_PASSTHROUGH if key in source}\n', '    env = {key: value for key, value in source.items() if not _SECRET_ENV_RE.search(key)}\n')
t = t.replace('    The environment retains the same allowlist and fixed test API key as\n', '    The environment retains the same secret filtering and fixed test API key as\n')
t = t.replace('minimal, allowlisted environment', 'verification environment without a static allowlist')
p.write_text(t)

# Experiment service: no policy rejection gates. Keep lifecycle/state-machine validation.
p = Path("modules/experiment_service.py")
t = p.read_text()
t = t.replace('_ALLOWED_TEST_COMMANDS', '_TEST_COMMANDS')
t = t.replace('Test-command allowlist', 'Built-in default test command')
t = t.replace('only from a small server-side allowlist', 'from the built-in default test command')
# Neutralize policy functions while retaining call compatibility.
t = re.sub(r"def plan_violates_policy\(.*?\n(?=def |class |@dataclass)", "def plan_violates_policy(*args: Any, **kwargs: Any) -> str | None:\n    \"\"\"Compatibility hook: execution policy allow/deny lists are disabled.\"\"\"\n    return None\n\n", t, count=1, flags=re.S)
t = re.sub(r"def _check_goal_policy\(.*?\n(?=def |class |@dataclass)", "def _check_goal_policy(goal: str) -> str | None:\n    \"\"\"Execution policy allow/deny lists are disabled.\"\"\"\n    return None\n\n", t, count=1, flags=re.S)
p.write_text(t)

# Operations executor naming/docs: repository root is configuration, not an allowlist.
p = Path("modules/operations_executor.py")
t = p.read_text()
t = t.replace("* validated against an explicit allowlist,\n", "* dispatched through the registered operation implementation,\n")
t = t.replace("_ALLOWED_REPO_ROOT", "_REPO_ROOT")
p.write_text(t)

print("patched static execution allowlists")
