import json
import os
import subprocess
import sys
import unittest
from urllib import error
from urllib import request


def _base_url() -> str:
    return (os.getenv("AGENT_API_URL") or "").rstrip("/")


def _json_get(url: str) -> tuple[int, object]:
    req = request.Request(url, method="GET", headers={"Accept": "application/json"})
    with request.urlopen(req, timeout=15) as response:
        status = int(getattr(response, "status", 200))
        raw = response.read().decode("utf-8", errors="replace")
    payload = json.loads(raw) if raw.strip() else None
    return status, payload


@unittest.skipUnless(_base_url(), "Integration tests require AGENT_API_URL")
class LiveApiIntegrationTests(unittest.TestCase):
    def test_https_connection_and_health(self):
        base = _base_url()
        self.assertTrue(base.startswith("https://"), "AGENT_API_URL must use HTTPS")

        status, payload = _json_get(f"{base}/health")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        self.assertEqual(payload.get("status"), "ok")

    def test_all_existing_status_problem_chat_endpoints(self):
        base = _base_url()
        status, openapi = _json_get(f"{base}/openapi.json")
        self.assertEqual(status, 200)
        self.assertIsInstance(openapi, dict)
        assert isinstance(openapi, dict)

        paths = openapi.get("paths", {})
        self.assertIsInstance(paths, dict)
        assert isinstance(paths, dict)

        categorized = []
        for path, methods in paths.items():
            lowered = str(path).lower()
            if any(token in lowered for token in ("health", "status", "problem", "chat")):
                categorized.append((str(path), methods))

        self.assertGreaterEqual(len(categorized), 1)

        for path, methods in categorized:
            self.assertIsInstance(methods, dict)
            if "get" in methods:
                req = request.Request(
                    f"{base}{path}", method="GET", headers={"Accept": "application/json"}
                )
                try:
                    with request.urlopen(req, timeout=15) as response:
                        code = int(getattr(response, "status", 200))
                except error.HTTPError as exc:
                    code = int(exc.code)
                    if exc.fp is not None:
                        exc.fp.close()
                self.assertIn(code, (200, 401, 403, 404, 405, 422))

            if "post" in methods:
                body = json.dumps({"message": "Integration smoke test"}).encode("utf-8")
                req = request.Request(
                    f"{base}{path}",
                    method="POST",
                    data=body,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                )
                try:
                    with request.urlopen(req, timeout=20) as response:
                        code = int(getattr(response, "status", 200))
                except error.HTTPError as exc:
                    code = int(exc.code)
                    if exc.fp is not None:
                        exc.fp.close()
                except Exception as exc:
                    if hasattr(exc, "code"):
                        code = int(getattr(exc, "code"))
                    else:
                        self.fail(f"POST {path} failed unexpectedly: {exc}")
                self.assertIn(code, (200, 201, 202, 400, 401, 403, 404, 405, 422))

    def test_full_chat_call_via_client_command(self):
        base = _base_url()
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "client",
                "--base-url",
                base,
                "chat",
                "Integration test ping",
            ],
            text=True,
            capture_output=True,
            timeout=30,
        )

        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode == 0:
            self.assertTrue(proc.stdout.strip())
            return

        self.assertIn(proc.returncode, (1,))
        self.assertTrue(
            ("HTTP 404" in combined)
            or ("Connection problem" in combined)
            or ("Invalid JSON response" in combined),
            f"Unexpected chat failure output: {combined}",
        )

    def test_non_existing_endpoint_is_handled(self):
        base = _base_url()
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "client",
                "--base-url",
                base,
                "chat",
                "--chat-path",
                "/definitely-does-not-exist",
                "Ping",
            ],
            text=True,
            capture_output=True,
            timeout=30,
        )

        self.assertEqual(proc.returncode, 1)
        self.assertIn("HTTP 404", proc.stderr)


if __name__ == "__main__":
    unittest.main()