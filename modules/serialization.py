"""JSON serialization helpers for agent results and tool payloads."""

import json
from typing import Any


def to_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def to_json_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(to_jsonable(value), ensure_ascii=False)
