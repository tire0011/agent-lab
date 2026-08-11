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

    def test_chat_respects_explicit_chat_path(self):
        with patch.object(
            client, "send_chat_message", return_value={"reply": "ok"}
        ) as mock_send_chat_message, patch("sys.stdout", io.StringIO()):
            code = client.main(
                [
                    "--base-url",
                    "http://127.0.0.1:8000",
                    "chat",
                    "--chat-path",
                    "/custom-chat",
                    "Ping",
                ]
            )

        self.assertEqual(code, 0)
        mock_send_chat_message.assert_called_once_with(
            "http://127.0.0.1:8000", "/custom-chat", "Ping"
        )

    @patch("Client.client.request.urlopen")
    def test_request_json_returns_parsed_json(self, mock_urlopen):
        payload = {"status": "ok"}
        mock_urlopen.return_value = _FakeResponse(json.dumps(payload), status=200)

        status, body = client.request_json("GET", "http://127.0.0.1:8000/health")

        self.assertEqual(status, 200)
        self.assertEqual(body, payload)

    @patch("Client.client.request.urlopen")
    def test_request_json_reports_connection_problem(self, mock_urlopen):
        mock_urlopen.side_effect = error.URLError("connection refused")

        with self.assertRaises(client.ClientError) as ctx:
            client.request_json("GET", "http://127.0.0.1:8000/health")

        self.assertIn("Connection problem", str(ctx.exception))

    @patch("Client.client.request.urlopen")
    def test_request_json_reports_invalid_json(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse("<html>not-json</html>", status=200)

        with self.assertRaises(client.ClientError) as ctx:
            client.request_json("GET", "http://127.0.0.1:8000/health")

        self.assertIn("Invalid JSON response", str(ctx.exception))

    @patch("Client.client.request.urlopen")
    def test_health_command_returns_error_code_on_connection_failure(self, mock_urlopen):
        mock_urlopen.side_effect = error.URLError("connection refused")
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            code = client.main(["--base-url", "http://127.0.0.1:9999", "health"])

        self.assertEqual(code, 1)
        self.assertIn("Connection problem", stderr.getvalue())

    @patch("Client.client.request.urlopen")
    def test_problems_command_prints_payload(self, mock_urlopen):
        payload = {"count": 1, "problems": [{"id": "x", "summary": "boom"}]}
        mock_urlopen.return_value = _FakeResponse(json.dumps(payload), status=200)
        stdout = io.StringIO()

        with patch("sys.stdout", stdout):
            code = client.main(["--base-url", "http://127.0.0.1:8000", "problems"])

        self.assertEqual(code, 0)
        self.assertIn("boom", stdout.getvalue())

    @patch("Client.client.request.urlopen")
    def test_todos_command_prints_payload(self, mock_urlopen):
        payload = {"count": 1, "todos": [{"id": "todo-1", "text": "do the thing"}]}
        mock_urlopen.return_value = _FakeResponse(json.dumps(payload), status=200)
        stdout = io.StringIO()

        with patch("sys.stdout", stdout):
            code = client.main(["--base-url", "http://127.0.0.1:8000", "todos"])

        self.assertEqual(code, 0)
        self.assertIn("do the thing", stdout.getvalue())

    @patch("Client.client.request.urlopen")
    def test_status_command_includes_agent_status_endpoint(self, mock_urlopen):
        health_payload = {"status": "ok"}
        agent_status_payload = {
            "agent_status": "idle",
            "api_version": "0.2.0",
            "components": ["health", "status", "problems", "todos", "chat"],
        }
        mock_urlopen.side_effect = [
            _FakeResponse(json.dumps(health_payload), status=200),
            _FakeResponse(json.dumps(agent_status_payload), status=200),
        ]
        stdout = io.StringIO()

        with patch.dict(os.environ, {}, clear=True), patch("sys.stdout", stdout):
            code = client.main(["--base-url", "http://127.0.0.1:8000", "status"])

        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("Agent status:", output)
        self.assertIn("api_version", output)


if __name__ == "__main__":
    unittest.main()
