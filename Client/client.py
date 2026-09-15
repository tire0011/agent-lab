import argparse
import json
import os
import sys
from typing import Any
from urllib import error, request


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_CHAT_PATH = "/chat"

# ---------------------------------------------------------------------------
# Codex task API client constants.
#
# The CLI is a thin HTTP client: it never imports CodexWorker/CodexTaskService
# and never chooses working_dir/sandbox (both are server-enforced). These
# constants mirror the server-side limits documented in api.py so the CLI can
# size its own HTTP timeout safely without an extra round trip.
# ---------------------------------------------------------------------------
CODEX_SERVER_MAX_TIMEOUT_SECONDS = 120  # mirrors api.py CODEX_MAX_TIMEOUT_SECONDS
CODEX_RUN_TIMEOUT_MARGIN_SECONDS = int(
    os.getenv("AGENT_CODEX_RUN_TIMEOUT_MARGIN_SECONDS", "15")
)
CODEX_TERMINAL_FAILURE_STATUSES = {"failed", "timed_out"}

# Exit code convention (documented in README.md):
#   0 = success (request succeeded; for run/execute the task also reached a
#       non-failure terminal status, e.g. 'succeeded')
#   1 = HTTP/transport-level failure (connection, timeout, 4xx/5xx, bad JSON)
#   2 = client-side usage error (e.g. missing API key)
#   3 = the HTTP call succeeded but the task's own terminal status is
#       'failed' or 'timed_out' (a real, but non-transport, failure)
EXIT_OK = 0
EXIT_HTTP_ERROR = 1
EXIT_USAGE_ERROR = 2
EXIT_TASK_TERMINAL_FAILURE = 3


class ClientError(Exception):
    """Raised for any failed request.

    ``http_status`` is set when the server returned a definitive HTTP
    response (e.g. 401/404/409/422/500): the outcome is known, just
    unsuccessful. It is left ``None`` for genuine transport-level failures
    (connection refused, client-side timeout, malformed JSON) where whether
    the server-side action happened at all is ambiguous.
    """

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


def resolve_base_url(cli_value: str | None) -> str:
    value = cli_value or os.getenv("AGENT_API_BASE_URL") or DEFAULT_BASE_URL
    return value.rstrip("/")


def resolve_chat_path(cli_value: str | None) -> str:
    path = cli_value or os.getenv("AGENT_API_CHAT_PATH") or DEFAULT_CHAT_PATH
    if not path.startswith("/"):
        path = f"/{path}"
    return path


def request_json(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any] | list[Any] | str]:
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    data: bytes | None = None
    if body is not None:
        req_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")

    req = request.Request(url=url, method=method.upper(), data=data, headers=req_headers)

    try:
        with request.urlopen(req, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            payload_raw = response.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        detail = raw.strip() or exc.reason
        raise ClientError(
            f"HTTP {exc.code} for {method.upper()} {url}: {detail}",
            http_status=exc.code,
        ) from exc
    except error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise ClientError(
            f"Connection problem for {method.upper()} {url}: {reason}"
        ) from exc

    if not payload_raw.strip():
        return status, {}

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError as exc:
        raise ClientError(
            f"Invalid JSON response from {method.upper()} {url}: {payload_raw[:200]}"
        ) from exc

    return status, payload


def find_chat_path(base_url: str, fallback_path: str) -> str:
    openapi_url = f"{base_url}/openapi.json"
    try:
        _, spec = request_json("GET", openapi_url)
    except ClientError:
        return fallback_path

    if not isinstance(spec, dict):
        return fallback_path

    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return fallback_path

    for path, operations in paths.items():
        if "chat" not in str(path).lower():
            continue
        if isinstance(operations, dict) and "post" in operations:
            return str(path)

    return fallback_path


def build_chat_body(operation: dict[str, Any] | None, message: str) -> dict[str, Any]:
    if not isinstance(operation, dict):
        return {"message": message}

    request_body = operation.get("requestBody")
    if not isinstance(request_body, dict):
        return {"message": message}

    content = request_body.get("content")
    if not isinstance(content, dict):
        return {"message": message}

    app_json = content.get("application/json")
    if not isinstance(app_json, dict):
        return {"message": message}

    schema = app_json.get("schema")
    if not isinstance(schema, dict):
        return {"message": message}

    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return {"message": message}

    preferred_names = ["message", "prompt", "input", "text"]
    for name in preferred_names:
        if name in properties:
            return {name: message}

    first_name = next(iter(properties.keys()))
    return {str(first_name): message}


def get_chat_operation(base_url: str, chat_path: str) -> dict[str, Any] | None:
    openapi_url = f"{base_url}/openapi.json"
    try:
        _, spec = request_json("GET", openapi_url)
    except ClientError:
        return None

    if not isinstance(spec, dict):
        return None

    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return None

    path_item = paths.get(chat_path)
    if not isinstance(path_item, dict):
        return None

    operation = path_item.get("post")
    if not isinstance(operation, dict):
        return None

    return operation


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_health(args: argparse.Namespace) -> int:
    base_url = resolve_base_url(args.base_url)
    url = f"{base_url}/health"

    try:
        status, payload = request_json("GET", url)
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"HTTP {status}")
    print_json(payload)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    base_url = resolve_base_url(args.base_url)
    exit_code = 0

    print("Public health:")
    try:
        public_status, public_payload = request_json("GET", f"{base_url}/health")
        print(f"  HTTP {public_status}")
        print_json(public_payload)
    except ClientError as exc:
        print(f"  {exc}", file=sys.stderr)
        exit_code = 1

    print("Agent status:")
    try:
        agent_status_code, agent_status_payload = request_json(
            "GET", f"{base_url}/status"
        )
        print(f"  HTTP {agent_status_code}")
        print_json(agent_status_payload)
    except ClientError as exc:
        print(f"  {exc}", file=sys.stderr)
        exit_code = 1

    api_key = args.api_key or os.getenv("AGENT_API_KEY")
    if not api_key:
        print("Secure health: skipped (no API key provided via --api-key or AGENT_API_KEY)")
        return exit_code

    print("Secure health:")
    try:
        secure_status, secure_payload = request_json(
            "GET",
            f"{base_url}/secure-health",
            headers={"x-api-key": api_key},
        )
        print(f"  HTTP {secure_status}")
        print_json(secure_payload)
    except ClientError as exc:
        print(f"  {exc}", file=sys.stderr)
        exit_code = 1

    return exit_code


def cmd_problems(args: argparse.Namespace) -> int:
    base_url = resolve_base_url(args.base_url)
    url = f"{base_url}/problems"

    try:
        status, payload = request_json("GET", url)
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"HTTP {status}")
    print_json(payload)
    return 0


def cmd_ideas(args: argparse.Namespace) -> int:
    base_url = resolve_base_url(args.base_url)
    url = f"{base_url}/ideas"

    try:
        status, payload = request_json("GET", url)
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"HTTP {status}")
    print_json(payload)
    return 0


def _resolve_api_key(args: argparse.Namespace) -> str | None:
    return args.api_key or os.getenv("AGENT_API_KEY")


def _require_api_key(args: argparse.Namespace) -> str | None:
    api_key = _resolve_api_key(args)
    if not api_key:
        print(
            "Missing API key. Provide --api-key or set AGENT_API_KEY.",
            file=sys.stderr,
        )
    return api_key


def _task_exit_code(task: Any) -> int:
    """Map a task's own terminal status to a CLI exit code.

    A successful HTTP response whose task ended in 'failed' or 'timed_out'
    is still a real failure from the caller's point of view (see README.md
    for the full exit-code convention), so it is surfaced as a non-zero
    exit code distinct from transport-level errors.
    """
    if isinstance(task, dict) and task.get("status") in CODEX_TERMINAL_FAILURE_STATUSES:
        return EXIT_TASK_TERMINAL_FAILURE
    return EXIT_OK


def _print_task_summary(task: dict[str, Any]) -> None:
    """Compact summary shown right after task creation."""
    print(f"  id:              {task.get('id')}")
    print(f"  status:          {task.get('status')}")
    print(f"  provider:        {task.get('provider')}")
    print(f"  timeout_seconds: {task.get('timeout_seconds')}")
    print(f"  sandbox:         {task.get('sandbox')}")
    print(f"  working_dir:     {task.get('working_dir')}")


def _print_task_details(task: dict[str, Any]) -> None:
    """Full, human-readable task state (used by get/run/execute)."""
    usage = task.get("usage")
    usage_display = json.dumps(usage, ensure_ascii=False) if usage is not None else None

    fields = [
        ("id", task.get("id")),
        ("status", task.get("status")),
        ("provider", task.get("provider")),
        ("prompt", task.get("prompt")),
        ("working_dir", task.get("working_dir")),
        ("sandbox", task.get("sandbox")),
        ("timeout_seconds", task.get("timeout_seconds")),
        ("created_at", task.get("created_at")),
        ("started_at", task.get("started_at")),
        ("ended_at", task.get("ended_at")),
        ("thread_id", task.get("thread_id")),
        ("exit_code", task.get("exit_code")),
        ("runtime_ms", task.get("runtime_ms")),
        ("timed_out", task.get("timed_out")),
        ("usage", usage_display),
        ("final_message", task.get("final_message")),
        ("error", task.get("error")),
    ]
    for name, value in fields:
        if value is None:
            value = "-"
        print(f"  {name}: {value}")


def _print_task_list_line(task: dict[str, Any]) -> None:
    """One compact, wrap-friendly line per task (narrow-terminal friendly)."""
    short_id = str(task.get("id", ""))[:8]
    status = task.get("status", "?")
    created_at = task.get("created_at", "-")
    runtime_ms = task.get("runtime_ms")
    runtime_display = f"{runtime_ms}ms" if runtime_ms is not None else "-"
    preview = task.get("prompt_preview") or ""
    print(f"{short_id}  {status:<10} created={created_at} runtime={runtime_display} prompt=\"{preview}\"")


def cmd_codex_create(args: argparse.Namespace) -> int:
    api_key = _require_api_key(args)
    if not api_key:
        return EXIT_USAGE_ERROR

    base_url = resolve_base_url(args.base_url)
    body: dict[str, Any] = {"prompt": args.prompt}
    if args.timeout_seconds is not None:
        body["timeout_seconds"] = args.timeout_seconds

    try:
        status, payload = request_json(
            "POST",
            f"{base_url}/codex/tasks",
            body=body,
            headers={"x-api-key": api_key},
        )
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_HTTP_ERROR

    if args.json:
        print_json(payload)
        return EXIT_OK if status == 201 else EXIT_HTTP_ERROR

    print(f"Task created (HTTP {status})")
    if isinstance(payload, dict):
        _print_task_summary(payload)
    else:
        print_json(payload)
    return EXIT_OK if status == 201 else EXIT_HTTP_ERROR


def cmd_codex_get(args: argparse.Namespace) -> int:
    api_key = _require_api_key(args)
    if not api_key:
        return EXIT_USAGE_ERROR

    base_url = resolve_base_url(args.base_url)
    url = f"{base_url}/codex/tasks/{args.task_id}"

    try:
        status, payload = request_json("GET", url, headers={"x-api-key": api_key})
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_HTTP_ERROR

    if args.json:
        print_json(payload)
        return _task_exit_code(payload)

    print(f"HTTP {status}")
    if isinstance(payload, dict):
        _print_task_details(payload)
        return _task_exit_code(payload)
    print_json(payload)
    return EXIT_OK


def cmd_codex_list(args: argparse.Namespace) -> int:
    api_key = _require_api_key(args)
    if not api_key:
        return EXIT_USAGE_ERROR

    base_url = resolve_base_url(args.base_url)
    url = f"{base_url}/codex/tasks?limit={args.limit}"

    try:
        status, payload = request_json("GET", url, headers={"x-api-key": api_key})
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_HTTP_ERROR

    if args.json:
        print_json(payload)
        return EXIT_OK

    if not isinstance(payload, dict):
        print_json(payload)
        return EXIT_OK

    count = payload.get("count", 0)
    tasks = payload.get("tasks", [])
    print(f"HTTP {status} ({count} task(s))")
    for task in tasks:
        _print_task_list_line(task)
    return EXIT_OK


def cmd_codex_run(args: argparse.Namespace) -> int:
    api_key = _require_api_key(args)
    if not api_key:
        return EXIT_USAGE_ERROR

    base_url = resolve_base_url(args.base_url)
    url = f"{base_url}/codex/tasks/{args.task_id}/run"
    # The stored task's exact timeout isn't known without an extra GET, so a
    # conservative HTTP timeout sized to the server's maximum is used; it is
    # always >= the task's actual server-side timeout, plus a small margin.
    http_timeout = CODEX_SERVER_MAX_TIMEOUT_SECONDS + CODEX_RUN_TIMEOUT_MARGIN_SECONDS

    print(
        f"Running task {args.task_id} synchronously; this may take up to "
        f"{CODEX_SERVER_MAX_TIMEOUT_SECONDS}s (server-enforced Codex timeout)..."
    )

    try:
        status, payload = request_json(
            "POST", url, headers={"x-api-key": api_key}, timeout=http_timeout
        )
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        if exc.http_status is None:
            print(
                f"Execution outcome for task {args.task_id} is unknown after a "
                f"transport-level failure. Inspect it with: codex get {args.task_id}",
                file=sys.stderr,
            )
        else:
            print(
                f"Task {args.task_id} was not started (server responded "
                f"HTTP {exc.http_status}). Inspect it with: codex get {args.task_id}",
                file=sys.stderr,
            )
        return EXIT_HTTP_ERROR

    if args.json:
        print_json(payload)
        return _task_exit_code(payload)

    print(f"HTTP {status}")
    if isinstance(payload, dict):
        _print_task_details(payload)
        return _task_exit_code(payload)
    print_json(payload)
    return EXIT_OK


def cmd_codex_execute(args: argparse.Namespace) -> int:
    api_key = _require_api_key(args)
    if not api_key:
        return EXIT_USAGE_ERROR

    base_url = resolve_base_url(args.base_url)
    body: dict[str, Any] = {"prompt": args.prompt}
    if args.timeout_seconds is not None:
        body["timeout_seconds"] = args.timeout_seconds

    try:
        create_status, create_payload = request_json(
            "POST",
            f"{base_url}/codex/tasks",
            body=body,
            headers={"x-api-key": api_key},
        )
    except ClientError as exc:
        # Creation itself failed at HTTP/transport level: no task exists,
        # nothing to run, and no retry is attempted.
        print(str(exc), file=sys.stderr)
        return EXIT_HTTP_ERROR

    if not isinstance(create_payload, dict) or not create_payload.get("id"):
        print("Task creation response did not include a task id.", file=sys.stderr)
        return EXIT_HTTP_ERROR

    task_id = create_payload["id"]
    # Print the task id immediately: if the following run call fails at the
    # transport level, the id must still be visible so the task can be
    # inspected later with `codex get`.
    print(f"Created task {task_id} (HTTP {create_status})")

    requested_timeout = create_payload.get("timeout_seconds") or CODEX_SERVER_MAX_TIMEOUT_SECONDS
    http_timeout = requested_timeout + CODEX_RUN_TIMEOUT_MARGIN_SECONDS

    print(
        f"Running task {task_id} synchronously; this may take up to "
        f"{requested_timeout}s (server-enforced Codex timeout)..."
    )

    try:
        run_status, run_payload = request_json(
            "POST",
            f"{base_url}/codex/tasks/{task_id}/run",
            headers={"x-api-key": api_key},
            timeout=http_timeout,
        )
    except ClientError as exc:
        # Transport-level failure while running: the task may or may not
        # have started on the server. Never create a second task as a retry;
        # point the caller at `codex get` to check the real outcome.
        print(str(exc), file=sys.stderr)
        if exc.http_status is None:
            print(
                f"Task {task_id} was created but its execution outcome is "
                f"unknown after a transport-level failure. Inspect it with: "
                f"codex get {task_id}",
                file=sys.stderr,
            )
        else:
            print(
                f"Task {task_id} was created but was not started (server "
                f"responded HTTP {exc.http_status}). Inspect it with: "
                f"codex get {task_id}",
                file=sys.stderr,
            )
        return EXIT_HTTP_ERROR

    if args.json:
        print_json(run_payload)
        return _task_exit_code(run_payload)

    print(f"HTTP {run_status}")
    if isinstance(run_payload, dict):
        _print_task_details(run_payload)
        return _task_exit_code(run_payload)
    print_json(run_payload)
    return EXIT_OK


def send_chat_message(base_url: str, chat_path: str, message: str) -> dict[str, Any] | list[Any] | str:
    operation = get_chat_operation(base_url, chat_path)
    payload = build_chat_body(operation, message)
    _, response_payload = request_json("POST", f"{base_url}{chat_path}", body=payload)
    return response_payload


def cmd_chat(args: argparse.Namespace) -> int:
    base_url = resolve_base_url(args.base_url)
    requested_chat_path = resolve_chat_path(args.chat_path)
    chat_path = (
        requested_chat_path
        if args.chat_path
        else find_chat_path(base_url, requested_chat_path)
    )

    if args.interactive:
        print(f"Interactive chat mode. Endpoint: {chat_path}")
        print("Type /quit to exit.")
        while True:
            try:
                line = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not line:
                continue
            if line.lower() in {"/quit", "quit", "exit"}:
                break

            try:
                payload = send_chat_message(base_url, chat_path, line)
            except ClientError as exc:
                print(str(exc), file=sys.stderr)
                return 1

            print("api>")
            print_json(payload)

        return 0

    if not args.message:
        print("Missing chat message. Provide text or use --interactive.", file=sys.stderr)
        return 2

    try:
        payload = send_chat_message(base_url, chat_path, args.message)
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print_json(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CLI client for the agent API")
    parser.add_argument(
        "--base-url",
        help="API base URL (overrides AGENT_API_BASE_URL)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    health_parser = subparsers.add_parser("health", help="Check public health endpoint")
    health_parser.set_defaults(func=cmd_health)

    status_parser = subparsers.add_parser(
        "status",
        help="Show public health, agent status (/status), and optional secure health",
    )
    status_parser.add_argument("--api-key", help="API key for /secure-health")
    status_parser.set_defaults(func=cmd_status)

    problems_parser = subparsers.add_parser(
        "problems", help="List currently detected problems (/problems)"
    )
    problems_parser.set_defaults(func=cmd_problems)

    ideas_parser = subparsers.add_parser(
        "ideas", help="List recorded ideas (/ideas)"
    )
    ideas_parser.set_defaults(func=cmd_ideas)

    chat_parser = subparsers.add_parser("chat", help="Send a message to chat endpoint")
    chat_parser.add_argument("message", nargs="?", help="Message text")
    chat_parser.add_argument(
        "--interactive",
        action="store_true",
        help="Start interactive chat mode",
    )
    chat_parser.add_argument(
        "--chat-path",
        help="Chat endpoint path (overrides AGENT_API_CHAT_PATH)",
    )
    chat_parser.set_defaults(func=cmd_chat)

    codex_parser = subparsers.add_parser(
        "codex", help="Manage Codex coding tasks (authenticated, synchronous, read-only)"
    )
    codex_subparsers = codex_parser.add_subparsers(dest="codex_command", required=True)

    # Shared, common Codex sub-arguments (API key, optional raw JSON output).
    codex_common = argparse.ArgumentParser(add_help=False)
    codex_common.add_argument("--api-key", help="API key (overrides AGENT_API_KEY)")
    codex_common.add_argument(
        "--json", action="store_true",
        help="Print the raw JSON response instead of a human-readable summary",
    )

    codex_create_parser = codex_subparsers.add_parser(
        "create", parents=[codex_common], help="Create a Codex task (POST /codex/tasks)"
    )
    codex_create_parser.add_argument(
        "--prompt", required=True, help="Natural-language coding task"
    )
    codex_create_parser.add_argument(
        "--timeout-seconds", type=int, default=None,
        help="Requested timeout in seconds (server enforces a fixed maximum)",
    )
    codex_create_parser.set_defaults(func=cmd_codex_create)

    codex_list_parser = codex_subparsers.add_parser(
        "list", parents=[codex_common], help="List recent Codex tasks (GET /codex/tasks)"
    )
    codex_list_parser.add_argument(
        "--limit", type=int, default=20, help="Max number of tasks to list (default: 20)"
    )
    codex_list_parser.set_defaults(func=cmd_codex_list)

    codex_get_parser = codex_subparsers.add_parser(
        "get", parents=[codex_common], help="Fetch a Codex task by id (GET /codex/tasks/{id})"
    )
    codex_get_parser.add_argument("task_id", help="Codex task id")
    codex_get_parser.set_defaults(func=cmd_codex_get)

    codex_run_parser = codex_subparsers.add_parser(
        "run", parents=[codex_common],
        help="Run a queued Codex task synchronously (POST /codex/tasks/{id}/run)",
    )
    codex_run_parser.add_argument("task_id", help="Codex task id")
    codex_run_parser.set_defaults(func=cmd_codex_run)

    codex_execute_parser = codex_subparsers.add_parser(
        "execute", parents=[codex_common],
        help="Create and immediately run one Codex task (convenience: create + run)",
    )
    codex_execute_parser.add_argument(
        "--prompt", required=True, help="Natural-language coding task"
    )
    codex_execute_parser.add_argument(
        "--timeout-seconds", type=int, default=None,
        help="Requested timeout in seconds (server enforces a fixed maximum)",
    )
    codex_execute_parser.set_defaults(func=cmd_codex_execute)

    return parser



def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
