from __future__ import annotations

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


