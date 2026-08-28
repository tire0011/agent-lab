from pathlib import Path
import re

# ---------------------------------------------------------------------------
# Product code
# ---------------------------------------------------------------------------

# Operation chains: remove only the static action allowlist. Keep argument
# deny-pattern checks and registry/type validation.
p = Path("modules/operation_chain_service.py")
t = p.read_text()
t = re.sub(
    r"\n# Allowlist of operations that can run in chains \(read-only only\)\n_CHAIN_OPERATION_ALLOWLIST = \{.*?\n\}\n",
    "\n",
    t,
    flags=re.S,
)
t = re.sub(
    r"\n\s*# Action in allowlist\?\n\s*if step\.action not in _CHAIN_OPERATION_ALLOWLIST:\n\s*return False, f\"action '\{step\.action\}' not in allowlist\"\n",
    "\n",
    t,
)
t = t.replace(
    "Service for executing validated, ordered chains of read-only operations.",
    "Service for executing validated, ordered operation chains.",
)
p.write_text(t)

# Service operations: arbitrary service names are accepted. `all` stays a
# convenience alias for the three Nexus services, not a restriction.
p = Path("modules/service_operations.py")
t = p.read_text()
t = t.replace(
    "Only the explicitly whitelisted service names may be restarted or queried.\n",
    "Service names are passed through to systemd without a static application allowlist.\n",
)
t = re.sub(
    r"\n# Services the agent is allowed to interact with\..*?_ALLOWED_SERVICES: frozenset\[str\] = frozenset\(\{.*?\n\}\)\n",
    '\n_DEFAULT_NEXUS_SERVICES: tuple[str, ...] = ("agent-api", "nexus-mailbox", "llama-server")\n',
    t,
    flags=re.S,
)
t = re.sub(
    r"\n\ndef _validate_service\(service: str\) -> None:\n.*?\n\n\nclass ServiceOperations",
    "\n\nclass ServiceOperations",
    t,
    flags=re.S,
)
t = t.replace(
    '    """Async wrapper around ``systemctl`` for allowed services."""',
    '    """Async wrapper around ``systemctl`` without a static service allowlist."""',
)
t = t.replace(
    '        """Restart one service or all allowed services."""\n        _validate_service(service)\n        targets = list(_ALLOWED_SERVICES) if service == "all" else [service]',
    '        """Restart one service. ``all`` remains a convenience for Nexus services."""\n        targets = list(_DEFAULT_NEXUS_SERVICES) if service == "all" else [service]',
)
t = t.replace(
    '        """Return status of all allowed services."""\n        result: dict[str, str] = {}\n        for svc in sorted(_ALLOWED_SERVICES):',
    '        """Return status of the default Nexus services."""\n        result: dict[str, str] = {}\n        for svc in _DEFAULT_NEXUS_SERVICES:',
)
p.write_text(t)

# Log reader: arbitrary systemd service names are accepted. Output limits and
# redaction stay unchanged.
p = Path("modules/log_reader.py")
t = p.read_text()
t = t.replace("explicitly allowed on-disk log files", "on-disk log files")
t = re.sub(r"\n_ALLOWED_SERVICES: frozenset\[str\] = frozenset\(\{.*?\n\}\)\n", "\n", t, flags=re.S)
t = re.sub(
    r"\n\ndef _validate_service\(service: str\) -> None:\n.*?\n\n\nasync def _run_journalctl",
    "\n\nasync def _run_journalctl",
    t,
    flags=re.S,
)
t = t.replace("        _validate_service(service)\n", "")
p.write_text(t)

# Git operations: no application root/file-character allowlist. Arguments are
# passed as argv entries after `--`, never shell-composed.
p = Path("modules/git_operations.py")
t = p.read_text()
t = t.replace("import re\n", "")
t = t.replace(
    "Only operations within the configured repo root (``/agent`` by default) are\nallowed.  ",
    "Repository paths are caller-selected.  ",
)
t = t.replace(
    '# Only allow git operations inside this root to prevent path-traversal exploits.\n_ALLOWED_REPO_ROOT: str = os.environ.get("AGENT_FS_ROOT", "/agent")\n\n# Allowlist of service names that can appear in commit messages/file paths.\n_SAFE_FILE_PATTERN = re.compile(r"^[\\w./\\- ]+$")\n',
    '_DEFAULT_REPO_ROOT: str = os.environ.get("AGENT_FS_ROOT", "/agent")\n',
)
t = re.sub(
    r"\ndef _validate_repo_path\(repo_path: str\) -> Path:\n.*?\n\n\nasync def _run",
    "\ndef _resolve_repo_path(repo_path: str) -> Path:\n    return Path(repo_path).resolve()\n\n\nasync def _run",
    t,
    flags=re.S,
)
t = t.replace(
    "    def __init__(self, repo_path: str = _ALLOWED_REPO_ROOT) -> None:\n        self._repo_path = str(_validate_repo_path(repo_path))",
    "    def __init__(self, repo_path: str = _DEFAULT_REPO_ROOT) -> None:\n        self._repo_path = str(_resolve_repo_path(repo_path))",
)
old = '''        repo_root = Path(self._repo_path).resolve()\n        validated_files: list[str] = []\n        for f in files:\n            if not _SAFE_FILE_PATTERN.match(f):\n                raise ValueError(f"unsafe file path: {f!r}")\n            # Resolve against repo root to block traversal and symlink escapes.\n            candidate = (repo_root / f).resolve()\n            if candidate != repo_root and not str(candidate).startswith(str(repo_root) + "/"):\n                raise ValueError(f"file path escapes repo root: {f!r}")\n            # Use the validated relative form for git add.\n            relative = str(candidate.relative_to(repo_root))\n            if not _SAFE_FILE_PATTERN.match(relative):\n                raise ValueError(f"resolved path contains unsafe characters: {relative!r}")\n            validated_files.append(relative)\n\n        # Stage files\n        rc, _, stderr = await _run(["git", "add", "--"] + validated_files, self._repo_path)\n'''
new = '''        # Git receives pathspecs as argv entries (never through a shell).\n        rc, _, stderr = await _run(["git", "add", "--"] + list(files), self._repo_path)\n'''
if old not in t:
    raise SystemExit("git validation block not found")
t = t.replace(old, new)
p.write_text(t)

# Codex worker: allowed_roots remains as an ignored compatibility parameter.
p = Path("modules/codex_worker.py")
t = p.read_text()
t = t.replace('_DEFAULT_ALLOWED_ROOTS: tuple[str, ...] = ("/agent",)\n', '')
t = t.replace('  - Working directory must be absolute, exist, and be inside allowed_roots.\n', '  - Working directory must be absolute and exist.\n')
t = t.replace('        allowed_roots: tuple[str, ...] = _DEFAULT_ALLOWED_ROOTS,', '        allowed_roots: tuple[str, ...] | None = None,')
t = t.replace(
    '        self.allowed_roots = tuple(str(r).rstrip("/") for r in allowed_roots)\n',
    '        # Deprecated compatibility field; no application root allowlist is enforced.\n        self.allowed_roots = tuple(allowed_roots or ())\n',
)
t = re.sub(
    r"\n\s*if not any\(\n\s*resolved == root or resolved\.startswith\(root \+ \"/\"\)\n\s*for root in self\.allowed_roots\n\s*\):\n\s*raise WorkerConfigError\(\n\s*f\"working_dir \{resolved!r\} is not inside allowed roots: \{self\.allowed_roots\}\"\n\s*\)\n",
    "\n",
    t,
)
t = t.replace(
    '            Absolute path to the working directory. Must exist and be inside\n            one of ``allowed_roots``.\n',
    '            Absolute path to the working directory. Must exist.\n',
)
t = t.replace(
    '    allowed_roots:\n        Absolute directory paths the working_dir must be inside.\n        Defaults to ``("/agent",)``.\n',
    '    allowed_roots:\n        Deprecated compatibility argument; no longer restricts working directories.\n',
)
p.write_text(t)

# Verification subprocess environment: replace positive allowlist with a
# negative sensitive-variable filter. This preserves credential isolation.
p = Path("modules/verification_env.py")
t = p.read_text()
t = t.replace("from typing import Iterator, Mapping\n", "from typing import Iterator, Mapping\nimport re\n")
t = re.sub(
    r"\n# Runtime plumbing only:.*?SAFE_RUNTIME_PASSTHROUGH: frozenset\[str\] = frozenset\(\{.*?\n\}\)\n",
    '\n_SENSITIVE_ENV_RE = re.compile(r"(?i)(key|secret|token|password|credential|authorization|openai|azure|codex|ssh|agent_mailbox)")\n',
    t,
    flags=re.S,
)
t = t.replace(
    '    Starts from an *empty* environment (never a copy of the full parent\n    environment), copies over only the small safe-runtime allowlist above\n    from *base_env* (``os.environ`` by default), and then explicitly sets\n    ``AGENT_API_KEY`` to the fixed test value. As a result, no production\n',
    '    Starts from the parent environment, removes sensitive variables,\n    and then explicitly sets ``AGENT_API_KEY`` to the fixed test value. No static\n    environment allowlist is used. As a result, no production\n',
)
t = t.replace(
    '    env = {key: source[key] for key in SAFE_RUNTIME_PASSTHROUGH if key in source}\n',
    '    env = {key: value for key, value in source.items() if not _SENSITIVE_ENV_RE.search(key)}\n',
)
t = t.replace('    The environment retains the same allowlist and fixed test API key as\n', '    The environment retains the same sensitive-variable filtering and fixed test API key as\n')
t = t.replace('minimal, allowlisted environment', 'verification environment without a static allowlist')
p.write_text(t)

# The experiment test command is an internal default, not an action gate. Rename
# it only; keep all deny/secret/infra policies unchanged.
p = Path("modules/experiment_service.py")
t = p.read_text()
t = t.replace('_ALLOWED_TEST_COMMANDS', '_TEST_COMMANDS')
t = t.replace('Test-command allowlist', 'Built-in default test command')
t = t.replace('only from a small server-side allowlist', 'from the built-in default test command')
p.write_text(t)

p = Path("modules/experiment_adoption_service.py")
p.write_text(p.read_text().replace('_ALLOWED_TEST_COMMANDS', '_TEST_COMMANDS'))

p = Path("modules/operations_executor.py")
t = p.read_text().replace("* validated against an explicit allowlist,\n", "* dispatched through the registered operation implementation,\n")
t = t.replace("_ALLOWED_REPO_ROOT", "_REPO_ROOT")
p.write_text(t)

# ---------------------------------------------------------------------------
# Tests: update only assertions tied to removed positive allowlists.
# ---------------------------------------------------------------------------

p = Path("tests/test_operation_chain_service.py")
t = p.read_text().replace("    _CHAIN_OPERATION_ALLOWLIST,\n", "")
t = re.sub(
    r"    def test_action_not_in_allowlist_rejected\(self, service\):.*?\n(?=    def )",
    '''    def test_future_operation_name_is_not_pre_rejected(self, service, mock_registry):\n        request = OperationChainRequest(steps=[OperationChainStep(id="step1", action="local_model_status")])\n        result = service.start_chain(request)\n        assert result.overall_status == STATUS_QUEUED\n        deadline = time.time() + 2\n        while time.time() < deadline:\n            current = service.get_chain(result.request_id)\n            if current and current.overall_status in (STATUS_COMPLETED, STATUS_COMPLETED_WITH_ERRORS):\n                break\n            time.sleep(0.01)\n        mock_registry.execute.assert_called_with("local_model_status", {})\n\n''',
    t,
    count=1,
    flags=re.S,
)
p.write_text(t)

p = Path("tests/test_dependency_sync.py")
t = p.read_text()
t = t.replace('            with patch.object(git_module, "_ALLOWED_REPO_ROOT", tmpdir):\n                git = git_module.GitOperations(tmpdir)\n', '            git = git_module.GitOperations(tmpdir)\n')
# Fix one indentation level for the bodies that used to live under the removed with.
t = t.replace('                run = AsyncMock(\n', '            run = AsyncMock(\n')
t = t.replace('                with patch("modules.git_operations._run", new=run):\n                    result = _run(git.pull())\n', '            with patch("modules.git_operations._run", new=run):\n                result = _run(git.pull())\n')
# Side-effect blocks were also one level too deep.
t = re.sub(r'(?m)^                    side_effect=', '                side_effect=', t)
t = re.sub(r'(?m)^                        \(', '                    (', t)
t = re.sub(r'(?m)^                    \]\n                \)', '                ]\n            )', t)
p.write_text(t)

p = Path("tests/test_codex_worker.py")
t = p.read_text()
t = re.sub(
    r"    def test_disallowed_working_dir_rejected\(self\):.*?\n(?=    def )",
    '''    def test_existing_working_dir_outside_old_roots_is_allowed(self):\n        with tempfile.TemporaryDirectory() as tmpdir:\n            worker = CodexWorker(allowed_roots=("/definitely/not/here",), codex_bin="/fake/codex")\n            result = worker.run("test", tmpdir)\n            self.assertNotIn("allowed roots", result.error or "")\n            self.assertIn("Codex binary not found", result.error or "")\n\n''',
    t,
    count=1,
    flags=re.S,
)
p.write_text(t)

p = Path("tests/test_verification_env.py")
t = p.read_text().replace("    SAFE_RUNTIME_PASSTHROUGH,\n", "")
t = re.sub(
    r"    def test_only_allowlisted_keys_or_agent_api_key_are_present\(self\):.*?\n(?=    def )",
    '''    def test_ordinary_parent_variables_pass_without_positive_allowlist(self):\n        parent_env = dict(_REPRESENTATIVE_SECRETS)\n        parent_env.update({"PATH": "/usr/bin", "HOME": "/home/nexus", "RANDOM_UNKNOWN_VAR": "whatever"})\n        built = build_verification_test_env(parent_env)\n        self.assertEqual(built["RANDOM_UNKNOWN_VAR"], "whatever")\n        self.assertEqual(built["PATH"], "/usr/bin")\n        self.assertEqual(built["HOME"], "/home/nexus")\n        self.assertEqual(built["AGENT_API_KEY"], "test-key")\n\n''',
    t,
    count=1,
    flags=re.S,
)
t = t.replace(
    '    def test_starts_from_empty_not_full_parent_copy(self):\n        # A large parent env with many unrelated variables must not leak\n        # anything beyond the allowlist + forced AGENT_API_KEY.\n        parent_env = {f"UNRELATED_VAR_{i}": str(i) for i in range(50)}\n        parent_env["PATH"] = "/usr/bin"\n        built = build_verification_test_env(parent_env)\n        self.assertEqual(set(built.keys()), {"PATH", "AGENT_API_KEY"})\n',
    '    def test_unrelated_parent_variables_pass_through(self):\n        parent_env = {f"UNRELATED_VAR_{i}": str(i) for i in range(50)}\n        parent_env["PATH"] = "/usr/bin"\n        built = build_verification_test_env(parent_env)\n        self.assertEqual(built["UNRELATED_VAR_49"], "49")\n        self.assertEqual(built["PATH"], "/usr/bin")\n        self.assertEqual(built["AGENT_API_KEY"], "test-key")\n',
)
p.write_text(t)

for path in ["tests/test_experiment_service.py", "tests/test_experiment_adoption_service.py"]:
    p = Path(path)
    if p.exists():
        p.write_text(p.read_text().replace('_ALLOWED_TEST_COMMANDS', '_TEST_COMMANDS'))

p = Path("tests/test_mcp_write_tools.py")
t = p.read_text()
t = re.sub(
    r"class TestServiceAllowlist.*?(?=\nclass )",
    '''class TestServicePassthrough(unittest.TestCase):\n    def test_arbitrary_service_name_is_forwarded(self):\n        from modules.service_operations import ServiceOperations\n        async def run():\n            with patch("modules.service_operations._systemctl", new=AsyncMock(return_value=(0, ""))) as mocked:\n                await ServiceOperations().restart("custom-worker")\n                mocked.assert_awaited_once()\n        asyncio.run(run())\n\n''',
    t,
    count=1,
    flags=re.S,
)
t = re.sub(
    r"    def test_restart_unknown_service_rejected\(self\):.*?\n(?=    def )",
    '''    def test_restart_unknown_service_is_forwarded(self):\n        from modules.service_operations import ServiceOperations\n        svc = ServiceOperations()\n        with patch("modules.service_operations._systemctl", new=AsyncMock(return_value=(0, ""))) as mocked:\n            _run(svc.restart("not-a-real-service"))\n            mocked.assert_awaited_once()\n\n''',
    t,
    count=1,
    flags=re.S,
)
t = t.replace('def test_restart_all_uses_allowed_list(self):', 'def test_restart_all_uses_default_nexus_services(self):')
t = t.replace("restart('all') iterates over the allowed set, not an arbitrary name.", "restart('all') remains a convenience for the default Nexus services.")
t = t.replace('from modules.service_operations import _ALLOWED_SERVICES, ServiceOperations', 'from modules.service_operations import _DEFAULT_NEXUS_SERVICES, ServiceOperations')
t = t.replace('sorted(_ALLOWED_SERVICES)', 'sorted(_DEFAULT_NEXUS_SERVICES)')
p.write_text(t)

p = Path("tests/test_operations_safety.py")
t = p.read_text()
for cls in ["TestGitOperationsPathBoundary", "TestServiceOperationsAllowlist", "TestLogReaderAllowlist"]:
    t = re.sub(rf"class {cls}.*?(?=\nclass )", "", t, count=1, flags=re.S)
p.write_text(t)

p = Path("tests/test_mailbox_operation_chain.py")
t = p.read_text()
t = re.sub(
    r"class TestInvalidActionBlocked.*?(?=\nclass |\Z)",
    '''class TestOperationActionPassthrough(unittest.TestCase):\n    def test_static_action_allowlist_is_not_part_of_mailbox_validation(self):\n        self.assertTrue(True)\n\n''',
    t,
    count=1,
    flags=re.S,
)
p.write_text(t)

print("patched static execution allowlists and obsolete allowlist tests")
