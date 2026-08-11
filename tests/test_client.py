import io
import json
import os
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from urllib import error

import Client.client as client


class _FakeResponse:
    def __init__(self, payload: str, status: int = 200):
        self._payload = payload.encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class ClientTests(unittest.TestCase):
    def test_resolve_base_url_prefers_cli_then_env_then_default(self):
        with patch.dict(os.environ, {"AGENT_API_BASE_URL": "http://env-host:9000"}, clear=False):
            self.assertEqual(client.resolve_base_url("http://cli-host:1234/"), "http://cli-host:1234")
            self.assertEqual(client.resolve_base_url(None), "http://env-host:9000")

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(client.resolve_base_url(None), client.DEFAULT_BASE_URL)

    @patch("client.request.urlopen")
    def test_request_json_returns_parsed_json(self, mock_urlopen):
        payload = {"status": "ok"}
        mock_urlopen.return_value = _FakeResponse(json.dumps(payload), status=200)

        status, body = client.request_json("GET", "http://127.0.0.1:8000/health")

        self.assertEqual(status, 200)
        self.assertEqual(body, payload)

    @patch("client.request.urlopen")
    def test_request_json_reports_connection_problem(self, mock_urlopen):
        mock_urlopen.side_effect = error.URLError("connection refused")

        with self.assertRaises(client.ClientError) as ctx:
            client.request_json("GET", "http://127.0.0.1:8000/health")

        self.assertIn("Connection problem", str(ctx.exception))

    @patch("client.request.urlopen")
    def test_request_json_reports_invalid_json(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse("<html>not-json</html>", status=200)

        with self.assertRaises(client.ClientError) as ctx:
            client.request_json("GET", "http://127.0.0.1:8000/health")

        self.assertIn("Invalid JSON response", str(ctx.exception))

    @patch("client.request.urlopen")
    def test_health_command_returns_error_code_on_connection_failure(self, mock_urlopen):
        mock_urlopen.side_effect = error.URLError("connection refused")
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            code = client.main(["--base-url", "http://127.0.0.1:9999", "health"])

        self.assertEqual(code, 1)
        self.assertIn("Connection problem", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()