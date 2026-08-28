from pathlib import Path
import re

# Agent client tests: no static tool filter, root defaults to filesystem root.
p = Path("tests/test_agent_clients.py")
p.write_text('''import os\nimport sys\nimport unittest\nfrom types import ModuleType\nfrom unittest.mock import patch, sentinel\n\n\nopenai_module = ModuleType("openai")\nopenai_module.AsyncOpenAI = object\nagents_module = ModuleType("agents")\nagents_module.__path__ = []\nagents_mcp_module = ModuleType("agents.mcp")\nagents_mcp_module.MCPServerStdio = object\n\nwith patch.dict(\n    sys.modules,\n    {\n        "openai": openai_module,\n        "agents": agents_module,\n        "agents.mcp": agents_mcp_module,\n    },\n):\n    from modules import agent_clients\n\nbuild_filesystem_server = agent_clients.build_filesystem_server\n\n\nclass BuildFilesystemServerTests(unittest.TestCase):\n    @patch.object(agent_clients, "MCPServerStdio")\n    def test_uses_default_command_and_filesystem_root(self, mock_mcp_server_stdio):\n        mock_mcp_server_stdio.return_value = sentinel.server\n        with patch.dict(os.environ, {}, clear=True):\n            server = build_filesystem_server()\n        mock_mcp_server_stdio.assert_called_once_with(\n            name="filesystem",\n            params={"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/"]},\n            cache_tools_list=True,\n        )\n        self.assertIs(server, sentinel.server)\n\n    @patch.object(agent_clients, "MCPServerStdio")\n    def test_agent_fs_root_no_longer_restricts_default_server(self, mock_mcp_server_stdio):\n        with patch.dict(os.environ, {"AGENT_FS_ROOT": "/workspace root"}, clear=True):\n            build_filesystem_server()\n        mock_mcp_server_stdio.assert_called_once_with(\n            name="filesystem",\n            params={"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/"]},\n            cache_tools_list=True,\n        )\n\n    @patch.object(agent_clients, "MCPServerStdio")\n    def test_parses_quoted_argument_override_with_shlex(self, mock_mcp_server_stdio):\n        environment = {\n            "MCP_FILESYSTEM_SERVER_COMMAND": "custom-mcp",\n            "MCP_FILESYSTEM_SERVER_ARGS": '--flag "value with spaces" \\\'single quoted\\\'',\n        }\n        with patch.dict(os.environ, environment, clear=True):\n            build_filesystem_server()\n        mock_mcp_server_stdio.assert_called_once_with(\n            name="filesystem",\n            params={"command": "custom-mcp", "args": ["--flag", "value with spaces", "single quoted"]},\n            cache_tools_list=True,\n        )\n\n    @patch.object(agent_clients, "MCPServerStdio")\n    def test_propagates_malformed_argument_error(self, mock_mcp_server_stdio):\n        with patch.dict(os.environ, {"MCP_FILESYSTEM_SERVER_ARGS": '--flag "unterminated'}, clear=True):\n            with self.assertRaises(ValueError):\n                build_filesystem_server()\n        mock_mcp_server_stdio.assert_not_called()\n\n    @patch.object(agent_clients, "MCPServerStdio")\n    def test_propagates_mcp_server_constructor_error(self, mock_mcp_server_stdio):\n        constructor_error = RuntimeError("constructor failed")\n        mock_mcp_server_stdio.side_effect = constructor_error\n        with patch.dict(os.environ, {}, clear=True):\n            with self.assertRaises(RuntimeError) as raised:\n                build_filesystem_server()\n        self.assertIs(raised.exception, constructor_error)\n\n\nif __name__ == "__main__":\n    unittest.main()\n''')

# Codex tests: remove the obsolete root keyword whether it appears in the
# defaults dict, before another kwarg, or as the final kwarg in a call.
p = Path("tests/test_codex_worker.py")
t = p.read_text()
t = re.sub(r'^\s*allowed_roots=\([^\n]*\),\n', '', t, flags=re.M)
t = re.sub(r'allowed_roots=\([^)]*\),\s*', '', t)
t = re.sub(r'allowed_roots=\([^)]*\)', '', t)
p.write_text(t)

# Maintenance tests: arbitrary service names and full process listing.
p = Path("tests/test_maintenance.py")
t = p.read_text()
t = t.replace('    ALLOWED_SERVICE_NAMES,\n', '')
t = re.sub(
    r'def test_service_name_allowlist_enforced\(monkeypatch\):.*?(?=\ndef test_service_logs)',
    '''def test_arbitrary_service_name_is_forwarded(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="active\\n", stderr="")
        result = svc.execute("service_status", "tok", {"service": "sshd"})
    assert result["service"] == "sshd"


''',
    t,
    count=1,
    flags=re.S,
)
t = re.sub(
    r'def test_service_logs_rejects_disallowed_service\(monkeypatch\):.*?(?=\n\n# ── Unit tests: process_status)',
    '''def test_service_logs_accepts_arbitrary_service(monkeypatch):
    _enabled_env(monkeypatch, token="tok")
    svc = _make_service()
    with patch("modules.maintenance_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="nginx log\\n", stderr="")
        result = svc.execute("service_logs", "tok", {"service": "nginx"})
    assert result["service"] == "nginx"

''',
    t,
    count=1,
    flags=re.S,
)
t = t.replace('def test_process_status_filters_output(monkeypatch):', 'def test_process_status_returns_unfiltered_output(monkeypatch):')
t = t.replace('    assert "/sbin/init" not in output\n', '    assert "/sbin/init" in output\n')
p.write_text(t)

print("updated tests for allowlist-free behavior")
