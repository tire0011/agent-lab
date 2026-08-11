import argparse
import json
import os
import sys
from typing import Any
from urllib import error, request


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_CHAT_PATH = "/chat"


class ClientError(Exception):
    pass


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
            f"HTTP {exc.code} for {method.upper()} {url}: {detail}"
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


def send_chat_message(base_url: str, chat_path: str, message: str) -> dict[str, Any] | list[Any] | str:
    operation = get_chat_operation(base_url, chat_path)
    payload = build_chat_body(operation, message)
    _, response_payload = request_json("POST", f"{base_url}{chat_path}", body=payload)
    return response_payload


def cmd_chat(args: argparse.Namespace) -> int:
    base_url = resolve_base_url(args.base_url)
    requested_chat_path = resolve_chat_path(args.chat_path)
    chat_path = find_chat_path(base_url, requested_chat_path)

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
        help="Show public and optional secure status/problem information",
    )
    status_parser.add_argument("--api-key", help="API key for /secure-health")
    status_parser.set_defaults(func=cmd_status)

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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())