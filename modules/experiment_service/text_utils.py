from __future__ import annotations

import json
import re
from datetime import datetime, timezone

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate_text(text: str | None, max_bytes: int, label: str = "output") -> str | None:
    if text is None:
        return None
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return (
        encoded[:max_bytes].decode("utf-8", errors="replace")
        + f"\n[... {label} truncated]"
    )


# Event ``item.type`` values considered safe and useful for diagnosing why a
# selection/reformat response could not be parsed. Reasoning content and
# unbounded tool output are intentionally excluded: they can be large, can
# reflect internal chain-of-thought, and are not needed to debug a JSON
# extraction failure (the "agent_message" text is what parse_plan() actually
# consumes). "command_execution" items are included only in a redacted,
# metadata-only form (command + exit_code + status), never their full
# ``aggregated_output``, since that could contain arbitrary file contents.
_DIAGNOSTIC_EVENT_TYPES: frozenset[str] = frozenset(
    {"thread.started", "turn.started", "turn.completed"}
)
_DIAGNOSTIC_ITEM_TYPES: frozenset[str] = frozenset({"agent_message"})
_DIAGNOSTIC_TOOL_ITEM_TYPES: frozenset[str] = frozenset({"command_execution"})

# Simple, conservative secret-scrubbing patterns applied to any diagnostic
# text before it is persisted. Matches common credential/token shapes
# regardless of which environment variable they came from.
_SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password|credential)\s*[:=]\s*\S+"),
    re.compile(r"sk-[A-Za-z0-9]{10,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{10,}"),
)


def _redact_secrets(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _extract_diagnostic_log(raw_stdout: str | None, max_bytes: int) -> str | None:
    """Build a small, redacted, size-limited diagnostic log from a raw
    Codex JSONL stdout stream.

    Only a safe subset of event/item types is kept (see
    ``_DIAGNOSTIC_EVENT_TYPES``/``_DIAGNOSTIC_ITEM_TYPES``/
    ``_DIAGNOSTIC_TOOL_ITEM_TYPES``); reasoning content and unbounded tool
    output are dropped entirely, and any residual secret-shaped text is
    scrubbed. The result is truncated to ``max_bytes`` with a clear marker
    (including the original byte count) if it does not fit.
    """
    if not raw_stdout or not raw_stdout.strip():
        return None

    kept_lines: list[str] = []
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        etype = event.get("type")
        if etype in _DIAGNOSTIC_EVENT_TYPES:
            kept_lines.append(json.dumps(event, sort_keys=True))
            continue

        if etype == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in _DIAGNOSTIC_ITEM_TYPES:
                kept_lines.append(json.dumps(event, sort_keys=True))
            elif item_type in _DIAGNOSTIC_TOOL_ITEM_TYPES:
                # Metadata-only: command + exit_code + status. Never the
                # full aggregated_output (may contain file contents/secrets).
                safe_item = {
                    "id": item.get("id"),
                    "type": item_type,
                    "command": item.get("command"),
                    "exit_code": item.get("exit_code"),
                    "status": item.get("status"),
                }
                kept_lines.append(
                    json.dumps({"type": "item.completed", "item": safe_item}, sort_keys=True)
                )
            # All other item types (e.g. "reasoning") are dropped entirely.

    if not kept_lines:
        return None

    log_text = _redact_secrets("\n".join(kept_lines))
    encoded = log_text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return log_text
    original_bytes = len(encoded)
    truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
    return (
        truncated
        + f"\n[... diagnostic log truncated; original size {original_bytes} bytes]"
    )
