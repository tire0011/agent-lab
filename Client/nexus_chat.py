from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib import error, request

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_POLL_INTERVAL = 2.0
TERMINAL_FAILURES = {"failed", "cancelled"}


class ChatClientError(RuntimeError):
    pass


def _request_json(
    method: str,
    url: str,
    *,
    api_key: str,
    body: dict[str, Any] | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "x-api-key": api_key,
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = request.Request(url=url, method=method, headers=headers, data=data)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise ChatClientError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except error.URLError as exc:
        raise ChatClientError(f"Verbindungsfehler: {getattr(exc, 'reason', exc)}") from exc
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ChatClientError(f"Ungültige JSON-Antwort: {raw[:200]}") from exc
    if not isinstance(payload, dict):
        raise ChatClientError("Unerwartete API-Antwort")
    return payload


def send_turn(
    base_url: str,
    api_key: str,
    message: str,
    *,
    session_id: str | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> tuple[dict[str, Any], str | None]:
    body: dict[str, Any] = {"message": message}
    if session_id:
        body["session_id"] = session_id

    submitted = _request_json("POST", f"{base_url}/chat", api_key=api_key, body=body)
    job_id = submitted.get("job_id")
    if not job_id:
        raise ChatClientError("Chat-Start lieferte keine job_id")

    while True:
        job = _request_json("GET", f"{base_url}/chat/jobs/{job_id}", api_key=api_key)
        status = str(job.get("status", ""))
        if status == "succeeded":
            return job, job.get("session_id") or session_id
        if status in TERMINAL_FAILURES:
            raise ChatClientError(str(job.get("error") or f"Chat-Job {status}"))
        time.sleep(max(0.2, poll_interval))


def print_reply(payload: dict[str, Any]) -> None:
    reply = payload.get("reply")
    if reply:
        print(f"nexus> {reply}")
    details = [payload.get("action"), payload.get("status")]
    if payload.get("experiment_id"):
        details.append(f"experiment={payload['experiment_id']}")
    details = [str(item) for item in details if item]
    if details:
        print(f"       [{' · '.join(details)}]")


def interactive(base_url: str, api_key: str, poll_interval: float) -> int:
    print(f"Nexus Chat ({base_url})")
    print("/quit beendet den Chat.\n")
    session_id: str | None = None
    while True:
        try:
            message = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not message:
            continue
        if message.lower() in {"/quit", "quit", "exit"}:
            return 0
        try:
            payload, session_id = send_turn(
                base_url,
                api_key,
                message,
                session_id=session_id,
                poll_interval=poll_interval,
            )
            print_reply(payload)
        except ChatClientError as exc:
            print(f"Fehler: {exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Interactive Nexus chat client")
    parser.add_argument(
        "--base-url",
        default=os.getenv("AGENT_API_BASE_URL", DEFAULT_BASE_URL),
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("AGENT_API_KEY"),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.api_key:
        print("AGENT_API_KEY fehlt.", file=sys.stderr)
        return 2
    return interactive(args.base_url.rstrip("/"), args.api_key, args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())
