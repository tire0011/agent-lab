import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal Azure OpenAI task runner")
    parser.add_argument("task", nargs="+", help="Task prompt to send")
    return parser.parse_args()


def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def extract_text(message_content: object) -> str:
    if isinstance(message_content, str):
        return message_content

    if isinstance(message_content, list):
        parts: list[str] = []
        for item in message_content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)

    return ""


def call_azure_openai(task: str) -> dict:
    endpoint = get_required_env("AZURE_OPENAI_ENDPOINT").rstrip("/")
    api_key = get_required_env("AZURE_OPENAI_API_KEY")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01")

    url = (
        f"{endpoint}/openai/deployments/{deployment}/chat/completions"
        f"?api-version={api_version}"
    )
    payload = {
        "messages": [{"role": "user", "content": task}],
        "temperature": 0,
    }

    req = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "api-key": api_key,
        },
    )

    try:
        with urllib.request.urlopen(req) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Azure OpenAI request failed ({err.code}): {body}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"Azure OpenAI connection failed: {err.reason}") from err


def log_run(entry: dict) -> None:
    runs_dir = Path("state/runs")
    runs_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{entry['timestamp'].replace(':', '-').replace('.', '-')}_{uuid4().hex}.json"
    filepath = runs_dir / filename
    filepath.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    task = " ".join(args.task).strip()
    if not task:
        print("Task cannot be empty.", file=sys.stderr)
        return 2

    started = time.perf_counter()
    timestamp = datetime.now(timezone.utc).isoformat()

    try:
        result = call_azure_openai(task)
        choice = (result.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        response_text = extract_text(message.get("content"))

        usage = result.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0)
        runtime_seconds = round(time.perf_counter() - started, 3)

        model_name = result.get("model") or os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
        entry = {
            "timestamp": timestamp,
            "model": model_name,
            "task": task,
            "response": response_text,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "runtime_seconds": runtime_seconds,
        }
        log_run(entry)

        print(response_text)
        return 0
    except Exception as exc:
        runtime_seconds = round(time.perf_counter() - started, 3)
        error_entry = {
            "timestamp": timestamp,
            "model": os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o"),
            "task": task,
            "response": f"ERROR: {exc}",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "runtime_seconds": runtime_seconds,
        }
        log_run(error_entry)
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
