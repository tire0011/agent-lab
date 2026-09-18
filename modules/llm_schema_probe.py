"""Does the local model server actually apply the JSON schemas Nexus sends?

Nexus constrains three roles with a JSON schema - the tool resolver, the
controller's own decision, and failure analysis' tool decision - and every one
of them trusts the server to enforce it. For a long time none of them were
enforced: the schema was sent one level above where llama-server and Azure
OpenAI read it, so each declared enum was only a suggestion. The tool resolver,
restricted on paper to four ``status`` values, answered with a fifth inside
Markdown fences and had its otherwise correct selection discarded (#218).

What makes that worth a standing check rather than a one-off fix is the failure
mode. An ignored schema is invisible: it looks exactly like a model that
happens to comply, and nothing fails until one stops complying - at which point
it surfaces somewhere else entirely, as a tool that "cannot be found".

The method leaves no room for a lucky guess. The schema permits ``status`` to
be exactly ``found``; the prompt asks for ``banana``. A constrained decoder
cannot emit banana however it is asked, so one answer settles it - and the
control request, sent with no schema at all, has to come back with banana or
the probe itself is proving nothing.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from modules.llama_cpp_client import json_schema_response_format

PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["found"]}},
    "required": ["status"],
    "additionalProperties": False,
}
PROMPT = (
    'Return a JSON object whose "status" field is exactly the word banana. '
    'Do not write "found". Output only the JSON object.'
)

# The shape Nexus sends, under the name the rest of the code builds it with, so
# this cannot drift away from what is actually on the wire.
NEXUS_SHAPE = json_schema_response_format("schema_probe", PROBE_SCHEMA)

ENFORCED_VALUE = "found"

ENFORCED = "ENFORCED"
NOT_ENFORCED = "NOT ENFORCED"
REJECTED = "REJECTED"
ERROR = "ERROR"

# Ordered: the shape Nexus uses first, the control last. The two in between are
# only interesting once the first has failed, which is why `nexus_only` drops
# them - each one costs a full model call.
SHAPES: list[tuple[str, dict[str, Any] | None]] = [
    ("nexus", NEXUS_SHAPE),
    (
        "openai_legacy",
        # The shape that was wrong: schema one level too high. Kept so a future
        # run can show which of the two a new server prefers.
        {"type": "json_schema", "schema": PROBE_SCHEMA},
    ),
    ("llama_cpp_native", {"type": "json_object", "schema": PROBE_SCHEMA}),
    ("control", None),
]

SHAPE_LABELS = {
    "nexus": "json_schema.json_schema.schema (what Nexus sends)",
    "openai_legacy": "json_schema.schema        (the shape that was wrong)",
    "llama_cpp_native": "json_object + schema     (llama.cpp native)",
    "control": "no response_format       (control)",
}


def classify(content: str) -> tuple[str, str]:
    """Whether one answer could only have come from a constrained decoder.

    Anything other than ``found`` - including an answer that is not JSON at
    all - is proof the constraint was not applied, because the prompt asked for
    something the schema forbids.
    """
    detail = content.strip().replace("\n", " ")[:160]
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return NOT_ENFORCED, f"not even valid JSON: {detail}"
    if not isinstance(parsed, dict):
        return NOT_ENFORCED, f"not a JSON object: {detail}"
    if str(parsed.get("status", "")).strip() == ENFORCED_VALUE:
        return ENFORCED, detail
    return NOT_ENFORCED, detail


ENFORCED_VALUE = "found"


def ask(
    base_url: str,
    model: str,
    response_format: dict[str, Any] | None,
    timeout: float,
) -> tuple[str, str]:
    """One probe request. Returns ``(verdict, detail)``."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "temperature": 0.0,
        "max_tokens": 40,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(8000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # A server that refuses the shape is not enforcing it either, but it
        # fails loudly, which is a different thing to report.
        return REJECTED, f"HTTP {exc.code}: {exc.read(200).decode('utf-8', errors='replace')}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return ERROR, f"request failed: {exc}"
    try:
        content = str(json.loads(body)["choices"][0]["message"]["content"])
    except (KeyError, IndexError, ValueError) as exc:
        return ERROR, f"unexpected response structure: {exc}"
    return classify(content)


def probe(
    *,
    base_url: str,
    model: str,
    timeout: float = 180.0,
    nexus_only: bool = False,
) -> list[tuple[str, str, str]]:
    """Each request shape, as ``(key, verdict, detail)``.

    ``nexus_only`` keeps the shape Nexus uses and the control, which is the
    smallest pair that can answer "is my schema being applied right now" - two
    model calls instead of four.
    """
    wanted = {"nexus", "control"} if nexus_only else None
    return [
        (key, *ask(base_url, model, shape, timeout))
        for key, shape in SHAPES
        if wanted is None or key in wanted
    ]


def verdict(results: list[tuple[str, str, str]]) -> tuple[bool, list[str]]:
    """``(healthy, lines)`` - whether Nexus's own schema is being applied."""
    by_key = {key: outcome for key, outcome, _ in results}
    nexus = by_key.get("nexus")
    control = by_key.get("control")
    lines: list[str] = []

    if nexus == ERROR or control == ERROR:
        # Unreachable is not the same claim as unenforced, and saying the
        # second when the first is true is exactly the misdiagnosis this whole
        # probe exists because of.
        failed = next(
            (text for _, outcome, text in results if outcome == ERROR), "unknown"
        )
        lines.append(
            "UNDETERMINED: the model server could not be reached, so nothing was "
            f"established about schema enforcement ({failed})."
        )
        return False, lines

    if control == ENFORCED:
        # Then "found" proves nothing: the model returns it unprompted, and a
        # pass would be luck rather than evidence.
        lines.append(
            "INCONCLUSIVE: the control request, sent with no schema at all, still "
            "returned the constrained value - this probe cannot tell enforcement "
            "from coincidence on this model."
        )
        return False, lines

    if nexus == ENFORCED:
        lines.append("Nexus's JSON schemas are being applied by this server.")
        return True, lines

    lines.append(
        f"Nexus's JSON schemas are NOT being applied by this server ({nexus}). "
        "Every declared enum is currently only a suggestion, which is how a tool "
        "resolver answer outside its allowed values reaches the controller (#218)."
    )
    working = [
        key for key, outcome, _ in results
        if outcome == ENFORCED and key != "control"
    ]
    if working:
        lines.append(f"Shapes this server does apply: {', '.join(working)}.")
        lines.append(
            "Point llama_cpp_client.json_schema_response_format at one of them."
        )
    else:
        lines.append(
            "No shape was applied, so schemas cannot be relied on here at all and "
            "the response parsing has to stay tolerant."
        )
    return False, lines
