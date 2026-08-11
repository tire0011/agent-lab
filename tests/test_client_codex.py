"""Unit tests for the Codex CLI commands in Client/client.py.

All HTTP calls are mocked via ``Client.client.request.urlopen`` (matching the
import alias used by this module). No FastAPI app is started, no real Codex
process is invoked, and no Codex quota is consumed.
"""

import io
import json
import os
import socket
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from urllib import error

os.environ.setdefault("AGENT_API_KEY", "test-key")

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


def _http_error(url: str, code: int, detail: dict | str = "") -> error.HTTPError:
    body = json.dumps(detail) if isinstance(detail, dict) else str(detail)
    return error.HTTPError(url, code, body, hdrs=None, fp=io.BytesIO(body.encode("utf-8")))


def _task_payload(**overrides) -> dict:
    defaults = dict(
        id="task-id-123",
        status="queued",
        prompt="Inspect README.md and summarize the project.",
        working_dir="/agent",
        sandbox="read-only",
        timeout_seconds=120,
        created_at="2026-08-11T12:00:00+00:00",
        started_at=None,
        ended_at=None,
        thread_id=None,
        exit_code=None,
        runtime_ms=None,
        timed_out=None,
        usage=None,
        final_message=None,
        error=None,
    )
    defaults.update(overrides)
    return defaults


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = client.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


BASE_ARGS = ["--base-url", "http://127.0.0.1:8000"]


class CreateCommandTests(unittest.TestCase):
    @patch("Client.client.request.urlopen")
    def test_create_sends_auth_header_and_prompt_only_by_default(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse(json.dumps(_task_payload()), status=201)

        code, stdout, _ = _run_cli(
            BASE_ARGS + ["codex", "create", "--prompt", "Inspect README.md", "--api-key", "secret-key"]
        )

        self.assertEqual(code, 0)
        sent_request = mock_urlopen.call_args[0][0]
        self.assertEqual(sent_request.get_header("X-api-key"), "secret-key")
        body = json.loads(sent_request.data.decode("utf-8"))
        self.assertEqual(body, {"prompt": "Inspect README.md"})
        self.assertIn("task-id-123", stdout)
        self.assertIn("queued", stdout)

    @patch("Client.client.request.urlopen")
    def test_create_with_optional_timeout(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse(
            json.dumps(_task_payload(timeout_seconds=30)), status=201
        )

        code, stdout, _ = _run_cli(
            BASE_ARGS
            + ["codex", "create", "--prompt", "do something", "--timeout-seconds", "30", "--api-key", "k"]
        )

        self.assertEqual(code, 0)
        sent_request = mock_urlopen.call_args[0][0]
        body = json.loads(sent_request.data.decode("utf-8"))
        self.assertEqual(body, {"prompt": "do something", "timeout_seconds": 30})
        self.assertIn("30", stdout)

    def test_create_has_no_working_dir_or_sandbox_arguments(self):
        parser = client.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                BASE_ARGS
                + ["codex", "create", "--prompt", "x", "--working-dir", "/tmp", "--api-key", "k"]
            )
        with self.assertRaises(SystemExit):
            parser.parse_args(
                BASE_ARGS + ["codex", "create", "--prompt", "x", "--sandbox", "workspace-write", "--api-key", "k"]
            )

    @patch("Client.client.request.urlopen")
    def test_create_prints_summary_fields(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse(json.dumps(_task_payload()), status=201)

        code, stdout, _ = _run_cli(
            BASE_ARGS + ["codex", "create", "--prompt", "Inspect README.md", "--api-key", "k"]
        )

        self.assertEqual(code, 0)
        for expected in ["id:", "status:", "timeout_seconds:", "sandbox:", "working_dir:"]:
            self.assertIn(expected, stdout)

    def test_create_missing_api_key_is_usage_error(self):
        with patch.dict(os.environ, {}, clear=True):
            code, _, stderr = _run_cli(
                BASE_ARGS + ["codex", "create", "--prompt", "do something"]
            )
        self.assertEqual(code, client.EXIT_USAGE_ERROR)
        self.assertIn("API key", stderr)


class GetCommandTests(unittest.TestCase):
    @patch("Client.client.request.urlopen")
    def test_get_sends_expected_request(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse(
            json.dumps(_task_payload(id="abc123", status="succeeded")), status=200
        )

        code, stdout, _ = _run_cli(BASE_ARGS + ["codex", "get", "abc123", "--api-key", "k"])

        self.assertEqual(code, 0)
        sent_request = mock_urlopen.call_args[0][0]
        self.assertTrue(sent_request.full_url.endswith("/codex/tasks/abc123"))
        self.assertEqual(sent_request.get_header("X-api-key"), "k")
        self.assertIn("succeeded", stdout)

    @patch("Client.client.request.urlopen")
    def test_get_displays_final_message_and_usage(self, mock_urlopen):
        payload = _task_payload(
            status="succeeded",
            final_message="agent-lab summary here",
            usage={"input_tokens": 10, "output_tokens": 5},
            thread_id="tid-1",
            runtime_ms=500,
        )
        mock_urlopen.return_value = _FakeResponse(json.dumps(payload), status=200)

        code, stdout, _ = _run_cli(BASE_ARGS + ["codex", "get", "abc123", "--api-key", "k"])

        self.assertEqual(code, 0)
        self.assertIn("agent-lab summary here", stdout)
        self.assertIn("input_tokens", stdout)
        self.assertIn("tid-1", stdout)
        self.assertIn("500", stdout)

    @patch("Client.client.request.urlopen")
    def test_get_marks_failed_status_with_nonzero_exit(self, mock_urlopen):
        payload = _task_payload(status="failed", error="boom")
        mock_urlopen.return_value = _FakeResponse(json.dumps(payload), status=200)

        code, stdout, _ = _run_cli(BASE_ARGS + ["codex", "get", "abc123", "--api-key", "k"])

        self.assertEqual(code, client.EXIT_TASK_TERMINAL_FAILURE)
        self.assertIn("failed", stdout)
        self.assertIn("boom", stdout)

    @patch("Client.client.request.urlopen")
    def test_get_marks_timed_out_status_with_nonzero_exit(self, mock_urlopen):
        payload = _task_payload(status="timed_out", timed_out=True)
        mock_urlopen.return_value = _FakeResponse(json.dumps(payload), status=200)

        code, stdout, _ = _run_cli(BASE_ARGS + ["codex", "get", "abc123", "--api-key", "k"])

        self.assertEqual(code, client.EXIT_TASK_TERMINAL_FAILURE)
        self.assertIn("timed_out", stdout)

    @patch("Client.client.request.urlopen")
    def test_get_unknown_task_returns_404(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(
            "http://127.0.0.1:8000/codex/tasks/nope", 404, {"detail": "task not found"}
        )

        code, _, stderr = _run_cli(BASE_ARGS + ["codex", "get", "nope", "--api-key", "k"])

        self.assertEqual(code, client.EXIT_HTTP_ERROR)
        self.assertIn("404", stderr)


class ListCommandTests(unittest.TestCase):
    @patch("Client.client.request.urlopen")
    def test_list_sends_limit_query_param(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse(json.dumps({"count": 0, "tasks": []}), status=200)

        code, _, _ = _run_cli(BASE_ARGS + ["codex", "list", "--limit", "10", "--api-key", "k"])

        self.assertEqual(code, 0)
        sent_request = mock_urlopen.call_args[0][0]
        self.assertIn("limit=10", sent_request.full_url)

    @patch("Client.client.request.urlopen")
    def test_list_compact_output_omits_long_prompt_and_final_message(self, mock_urlopen):
        long_prompt_preview = "x" * 80 + "…"
        tasks = [
            {
                "id": "1111111122222222",
                "status": "succeeded",
                "working_dir": "/agent",
                "sandbox": "read-only",
                "timeout_seconds": 120,
                "created_at": "2026-08-11T12:00:00+00:00",
                "started_at": None,
                "ended_at": None,
                "thread_id": None,
                "exit_code": 0,
                "runtime_ms": 999,
                "timed_out": False,
                "error": None,
                "prompt_preview": long_prompt_preview,
            }
        ]
        mock_urlopen.return_value = _FakeResponse(
            json.dumps({"count": 1, "tasks": tasks}), status=200
        )

        code, stdout, _ = _run_cli(BASE_ARGS + ["codex", "list", "--api-key", "k"])

        self.assertEqual(code, 0)
        self.assertIn("11111111", stdout)  # abbreviated id
        self.assertIn("succeeded", stdout)
        self.assertIn("999ms", stdout)
        self.assertIn(long_prompt_preview, stdout)
        # A full-length raw JSON dump of an unrelated huge prompt/final_message
        # must not appear; the endpoint itself never sends them for list.
        self.assertNotIn('"final_message"', stdout)


class RunCommandTests(unittest.TestCase):
    @patch("Client.client.request.urlopen")
    def test_run_sends_expected_request(self, mock_urlopen):
        payload = _task_payload(status="succeeded", exit_code=0, runtime_ms=42)
        mock_urlopen.return_value = _FakeResponse(json.dumps(payload), status=200)

        code, stdout, _ = _run_cli(BASE_ARGS + ["codex", "run", "task-id-123", "--api-key", "k"])

        self.assertEqual(code, 0)
        sent_request = mock_urlopen.call_args[0][0]
        self.assertTrue(sent_request.full_url.endswith("/codex/tasks/task-id-123/run"))
        self.assertEqual(sent_request.get_method(), "POST")
        self.assertIn("succeeded", stdout)

    @patch("Client.client.request.urlopen")
    def test_run_warns_before_call_about_possible_wait(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse(
            json.dumps(_task_payload(status="succeeded")), status=200
        )

        _, stdout, _ = _run_cli(BASE_ARGS + ["codex", "run", "task-id-123", "--api-key", "k"])
        self.assertIn("may take up to", stdout)

    @patch("Client.client.request.urlopen")
    def test_run_conflict_is_not_retried(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(
            "http://x/codex/tasks/abc/run", 409, {"detail": "cannot start"}
        )

        code, _, stderr = _run_cli(BASE_ARGS + ["codex", "run", "abc", "--api-key", "k"])

        self.assertEqual(code, client.EXIT_HTTP_ERROR)
        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertIn("409", stderr)


class ExecuteCommandTests(unittest.TestCase):
    @patch("Client.client.request.urlopen")
    def test_execute_performs_exactly_one_create_and_one_run(self, mock_urlopen):
        create_payload = _task_payload(id="exec-task-1", status="queued")
        run_payload = _task_payload(id="exec-task-1", status="succeeded", exit_code=0)
        mock_urlopen.side_effect = [
            _FakeResponse(json.dumps(create_payload), status=201),
            _FakeResponse(json.dumps(run_payload), status=200),
        ]

        code, stdout, _ = _run_cli(
            BASE_ARGS + ["codex", "execute", "--prompt", "Inspect README.md", "--api-key", "k"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(mock_urlopen.call_count, 2)
        create_request = mock_urlopen.call_args_list[0][0][0]
        run_request = mock_urlopen.call_args_list[1][0][0]
        self.assertTrue(create_request.full_url.endswith("/codex/tasks"))
        self.assertTrue(run_request.full_url.endswith("/codex/tasks/exec-task-1/run"))
        self.assertIn("exec-task-1", stdout)
        self.assertIn("succeeded", stdout)

    @patch("Client.client.request.urlopen")
    def test_execute_uses_id_returned_by_create(self, mock_urlopen):
        create_payload = _task_payload(id="specific-id-999", status="queued")
        run_payload = _task_payload(id="specific-id-999", status="succeeded")
        mock_urlopen.side_effect = [
            _FakeResponse(json.dumps(create_payload), status=201),
            _FakeResponse(json.dumps(run_payload), status=200),
        ]

        _run_cli(BASE_ARGS + ["codex", "execute", "--prompt", "x", "--api-key", "k"])

        run_request = mock_urlopen.call_args_list[1][0][0]
        self.assertIn("specific-id-999", run_request.full_url)

    @patch("Client.client.request.urlopen")
    def test_execute_does_not_run_when_create_fails(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error("http://x/codex/tasks", 422, {"detail": "bad prompt"})

        code, _, stderr = _run_cli(
            BASE_ARGS + ["codex", "execute", "--prompt", "", "--api-key", "k"]
        )

        self.assertEqual(code, client.EXIT_HTTP_ERROR)
        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertIn("422", stderr)

    @patch("Client.client.request.urlopen")
    def test_execute_does_not_create_second_task_after_run_failure(self, mock_urlopen):
        create_payload = _task_payload(id="task-abc", status="queued")
        mock_urlopen.side_effect = [
            _FakeResponse(json.dumps(create_payload), status=201),
            error.URLError("connection refused"),
        ]

        code, stdout, stderr = _run_cli(
            BASE_ARGS + ["codex", "execute", "--prompt", "x", "--api-key", "k"]
        )

        self.assertEqual(code, client.EXIT_HTTP_ERROR)
        self.assertEqual(mock_urlopen.call_count, 2)  # exactly one create + one run attempt
        # The task id must remain visible for later inspection via `codex get`.
        self.assertIn("task-abc", stdout)
        self.assertIn("task-abc", stderr)

    @patch("Client.client.request.urlopen")
    def test_execute_run_timeout_still_shows_task_id(self, mock_urlopen):
        create_payload = _task_payload(id="task-timeout-case", status="queued")
        mock_urlopen.side_effect = [
            _FakeResponse(json.dumps(create_payload), status=201),
            error.URLError(socket.timeout("timed out")),
        ]

        code, stdout, stderr = _run_cli(
            BASE_ARGS + ["codex", "execute", "--prompt", "x", "--api-key", "k"]
        )

        self.assertEqual(code, client.EXIT_HTTP_ERROR)
        self.assertIn("task-timeout-case", stdout)
        self.assertIn("codex get task-timeout-case", stderr)

    @patch("Client.client.request.urlopen")
    def test_execute_shows_failed_status_with_nonzero_exit(self, mock_urlopen):
        create_payload = _task_payload(id="task-fail", status="queued")
        run_payload = _task_payload(id="task-fail", status="failed", error="worker exploded")
        mock_urlopen.side_effect = [
            _FakeResponse(json.dumps(create_payload), status=201),
            _FakeResponse(json.dumps(run_payload), status=200),
        ]

        code, stdout, _ = _run_cli(
            BASE_ARGS + ["codex", "execute", "--prompt", "x", "--api-key", "k"]
        )

        self.assertEqual(code, client.EXIT_TASK_TERMINAL_FAILURE)
        self.assertIn("failed", stdout)
        self.assertIn("worker exploded", stdout)


class AuthErrorHandlingTests(unittest.TestCase):
    @patch("Client.client.request.urlopen")
    def test_unauthorized_401_is_reported_without_leaking_key(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error(
            "http://x/codex/tasks", 401, {"detail": "Unauthorized"}
        )

        code, stdout, stderr = _run_cli(
            BASE_ARGS + ["codex", "create", "--prompt", "x", "--api-key", "super-secret-key"]
        )

        self.assertEqual(code, client.EXIT_HTTP_ERROR)
        self.assertIn("401", stderr)
        self.assertNotIn("super-secret-key", stdout)
        self.assertNotIn("super-secret-key", stderr)

    @patch("Client.client.request.urlopen")
    def test_server_error_500_is_reported(self, mock_urlopen):
        mock_urlopen.side_effect = _http_error("http://x/codex/tasks", 500, "internal error")

        code, _, stderr = _run_cli(BASE_ARGS + ["codex", "create", "--prompt", "x", "--api-key", "k"])

        self.assertEqual(code, client.EXIT_HTTP_ERROR)
        self.assertIn("500", stderr)

    @patch("Client.client.request.urlopen")
    def test_connection_failure_is_reported(self, mock_urlopen):
        mock_urlopen.side_effect = error.URLError("connection refused")

        code, _, stderr = _run_cli(BASE_ARGS + ["codex", "list", "--api-key", "k"])

        self.assertEqual(code, client.EXIT_HTTP_ERROR)
        self.assertIn("Connection problem", stderr)

    @patch("Client.client.request.urlopen")
    def test_client_side_timeout_is_reported(self, mock_urlopen):
        mock_urlopen.side_effect = error.URLError(socket.timeout("timed out"))

        code, _, stderr = _run_cli(BASE_ARGS + ["codex", "run", "abc", "--api-key", "k"])

        self.assertEqual(code, client.EXIT_HTTP_ERROR)
        self.assertIn("timed out", stderr)

    @patch("Client.client.request.urlopen")
    def test_malformed_json_response_is_reported(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse("<not json>", status=200)

        code, _, stderr = _run_cli(BASE_ARGS + ["codex", "get", "abc", "--api-key", "k"])

        self.assertEqual(code, client.EXIT_HTTP_ERROR)
        self.assertIn("Invalid JSON response", stderr)

    def test_no_traceback_printed_for_expected_connection_error(self):
        with patch("Client.client.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = error.URLError("connection refused")
            code, _, stderr = _run_cli(BASE_ARGS + ["codex", "list", "--api-key", "k"])
        self.assertEqual(code, client.EXIT_HTTP_ERROR)
        self.assertNotIn("Traceback", stderr)


class ExistingCommandsStillWorkTests(unittest.TestCase):
    @patch("Client.client.request.urlopen")
    def test_health_command_still_works(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse(json.dumps({"status": "ok"}), status=200)
        code, stdout, _ = _run_cli(BASE_ARGS + ["health"])
        self.assertEqual(code, 0)
        self.assertIn("ok", stdout)

    @patch("Client.client.request.urlopen")
    def test_todos_command_still_works(self, mock_urlopen):
        payload = {"count": 1, "todos": [{"id": "t1", "text": "do it"}]}
        mock_urlopen.return_value = _FakeResponse(json.dumps(payload), status=200)
        code, stdout, _ = _run_cli(BASE_ARGS + ["todos"])
        self.assertEqual(code, 0)
        self.assertIn("do it", stdout)


if __name__ == "__main__":
    unittest.main()
