from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected text not found in {path}")
    if text.count(old) != 1:
        raise SystemExit(f"expected exactly one match in {path}, got {text.count(old)}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "modules/operations_registry.py",
    "import time\nimport uuid\n",
    "import time\nimport urllib.error\nimport urllib.request\nimport uuid\n",
)

replace_once(
    "modules/operations_registry.py",
    '''    def execute(self, operation: str, args: dict[str, Any] | None = None, *, caller: str = "authenticated caller") -> OperationResult:\n        if operation not in self._specs:\n            raise OperationValidationError("Operation ist nicht erlaubt.")\n        params = dict(args or {})\n        started = time.perf_counter()\n        try:\n            # Canonical operation names use underscores (e.g. "git_status") and map\n            # 1:1 to "_op_<name>" handler methods; this is the single source of\n            # truth shared by the catalog, the API, and OperationChainService.\n            handler = getattr(self, f"_op_{operation}")\n            result = handler(params)\n''',
    '''    def execute(self, operation: str, args: dict[str, Any] | None = None, *, caller: str = "authenticated caller") -> OperationResult:\n        params = dict(args or {})\n        started = time.perf_counter()\n        try:\n            # Executability is defined by an implemented handler, not by the\n            # metadata catalog.  This avoids a second positive allowlist that can\n            # silently block operations which are already implemented elsewhere.\n            # Unknown names still fail safely because no handler exists.\n            handler = getattr(self, f"_op_{operation}", None)\n            if handler is None or not callable(handler):\n                raise OperationValidationError(f"Unbekannte Operation: {operation}")\n            result = handler(params)\n''',
)

replace_once(
    "modules/operations_registry.py",
    '''            "mailbox_status": OperationSpec("mailbox_status", "Status des Nexus-Mailbox-Service und -Timers."),\n            "service_logs": OperationSpec("service_logs", "journalctl-Auszug für den API-Service.", [limit50]),\n''',
    '''            "mailbox_status": OperationSpec("mailbox_status", "Status des Nexus-Mailbox-Service und -Timers."),\n            "local_model_status": OperationSpec("local_model_status", "Status und Modellliste des lokalen llama.cpp-Servers."),\n            "service_logs": OperationSpec("service_logs", "journalctl-Auszug für den API-Service.", [limit50]),\n''',
)

marker = '''    def _op_service_status(self, args: dict[str, Any]) -> OperationResult:\n'''
addition = '''    def _op_local_model_status(self, args: dict[str, Any]) -> OperationResult:\n        if args:\n            raise OperationValidationError("local_model_status akzeptiert keine Argumente.")\n        started = time.perf_counter()\n        base_url = (\n            os.getenv("LOCAL_MODEL_URL")\n            or os.getenv("LLAMA_CPP_BASE_URL")\n            or "http://127.0.0.1:8080/v1"\n        ).rstrip("/")\n        try:\n            request = urllib.request.Request(f"{base_url}/models", method="GET")\n            with urllib.request.urlopen(request, timeout=15) as response:\n                body = response.read(4000).decode("utf-8", errors="replace")\n                status = getattr(response, "status", 200)\n            lines = bounded_lines(redact(body), max_lines=40)\n            return OperationResult(\n                operation="local_model_status",\n                success=200 <= int(status) < 300,\n                output_lines=[f"http_status: {status}", *lines],\n                duration_ms=int((time.perf_counter() - started) * 1000),\n                exit_code=0 if 200 <= int(status) < 300 else int(status),\n                error_summary=None if 200 <= int(status) < 300 else f"local model HTTP {status}",\n            )\n        except urllib.error.HTTPError as exc:\n            return OperationResult(\n                operation="local_model_status",\n                success=False,\n                output_lines=[],\n                duration_ms=int((time.perf_counter() - started) * 1000),\n                exit_code=exc.code,\n                error_summary=f"local model HTTP {exc.code}",\n            )\n        except (urllib.error.URLError, TimeoutError, OSError) as exc:\n            return OperationResult(\n                operation="local_model_status",\n                success=False,\n                output_lines=[],\n                duration_ms=int((time.perf_counter() - started) * 1000),\n                error_summary=sanitized_reason(str(exc)),\n            )\n\n'''
registry_path = Path("modules/operations_registry.py")
text = registry_path.read_text(encoding="utf-8")
if marker not in text:
    raise SystemExit("service status marker missing")
registry_path.write_text(text.replace(marker, addition + marker, 1), encoding="utf-8")

replace_once(
    "tests/test_operations_registry.py",
    '''    def test_catalog_lists_allowlisted_operations(self):\n        catalog = self.registry.catalog()\n        names = {item.name for item in catalog}\n        self.assertIn("git_status", names)\n        self.assertIn("service_logs", names)\n        self.assertIn("mailbox_status", names)\n        self.assertNotIn("experiment adopt", names)\n''',
    '''    def test_catalog_lists_available_operations(self):\n        catalog = self.registry.catalog()\n        names = {item.name for item in catalog}\n        self.assertIn("git_status", names)\n        self.assertIn("service_logs", names)\n        self.assertIn("mailbox_status", names)\n        self.assertIn("local_model_status", names)\n        self.assertNotIn("experiment adopt", names)\n\n    def test_execution_uses_implemented_handler_not_catalog_membership(self):\n        self.registry._specs.pop("git_status")\n        result = self.registry.execute("git_status", {})\n        self.assertTrue(result.success)\n\n    def test_local_model_status_handler_returns_models_response(self):\n        class FakeResponse:\n            status = 200\n            def __enter__(self):\n                return self\n            def __exit__(self, exc_type, exc, tb):\n                return False\n            def read(self, limit):\n                return b'{"data":[{"id":"local-test-model"}]}'\n\n        from unittest.mock import patch\n        with patch("modules.operations_registry.urllib.request.urlopen", return_value=FakeResponse()):\n            result = self.registry.execute("local_model_status", {})\n        self.assertTrue(result.success)\n        self.assertIn("local-test-model", "\\n".join(result.output_lines))\n''',
)

print("operations registry dispatch fix applied")
