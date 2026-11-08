from __future__ import annotations

import json
import re
from typing import Any

from .models import ExperimentPlan



_PLAN_FIELD_PATTERNS: dict[str, str] = {
    "title": r"title",
    "motivation": r"motivation",
    "affected_files": r"affected[_ ]files",
    "expected_changes": r"expected[_ ]changes",
    "success_criteria": r"success[_ ]criteria",
    "planned_tests": r"(planned[_ ]tests|tests)",
    "risks": r"risks",
    "estimated_scope": r"estimated[_ ]scope",
    "change_kind": r"change[_ ]kind",
    "required_artifacts": r"required[_ ]artifacts",
    "acceptance_criteria": r"acceptance[_ ]criteria",
}


def _find_balanced_json_objects(text: str) -> list[str]:
    """Scan *text* and return every top-level balanced ``{...}`` substring, tracking
    string/escape state so braces inside JSON string values never throw off the
    matching (nested objects stay part of their parent's span)."""
    objects: list[str] = []
    depth = 0
    start: int | None = None
    in_string = False
    escape = False

    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    objects.append(text[start : i + 1])
                    start = None

    return objects


def extract_single_json_object(raw_text: str | None) -> dict[str, Any] | None:
    """Tolerantly extract exactly one JSON object from *raw_text*: pure JSON first,
    then one object in a ```json fence, then one top-level balanced object.
    Returns ``None`` — never guesses or merges — when zero or several candidates
    are found or the object does not parse.
    """
    if not raw_text or not raw_text.strip():
        return None

    text = raw_text.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    fenced_blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if len(fenced_blocks) == 1:
        try:
            parsed = json.loads(fenced_blocks[0])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        return None
    if len(fenced_blocks) > 1:
        return None  # ambiguous: more than one candidate, do not guess

    candidates = _find_balanced_json_objects(text)
    if len(candidates) != 1:
        return None  # zero or ambiguous multiple objects: reject, don't guess

    try:
        parsed = json.loads(candidates[0])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_plan(raw_text: str | None) -> ExperimentPlan | None:
    """Best-effort parse of the selection phase's structured output: one unambiguous
    JSON object if present (``extract_single_json_object``), else ``Field: value``
    lines. Returns ``None`` when fields cannot be reliably extracted - callers
    must treat that as "reject, do not guess"."""
    if not raw_text or not raw_text.strip():
        return None

    text = raw_text.strip()
    data = extract_single_json_object(text)

    if data is None:
        # Fallback: "Field: value" lines, only when no JSON object was present;
        # in the ambiguous case this fails to find clean fields too, which is
        # the desired conservative behavior.
        data = {}
        for line in text.splitlines():
            match = re.match(r"^\s*[-*]?\s*([A-Za-z_ ]+):\s*(.+)$", line)
            if not match:
                continue
            key_raw, value = match.groups()
            for field, pattern in _PLAN_FIELD_PATTERNS.items():
                if re.fullmatch(pattern, key_raw.strip().lower().replace(" ", "_")):
                    data[field] = value.strip()
                    break

    if not data:
        return None

    def _get_str(key: str) -> str | None:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return None

    affected_files_raw = data.get("affected_files")
    if isinstance(affected_files_raw, list):
        affected_files = [str(v).strip() for v in affected_files_raw if str(v).strip()]
    elif isinstance(affected_files_raw, str) and affected_files_raw.strip():
        affected_files = [p.strip() for p in re.split(r"[,\n]", affected_files_raw) if p.strip()]
    else:
        affected_files = []

    required = {
        "title": _get_str("title"),
        "motivation": _get_str("motivation"),
        "expected_changes": _get_str("expected_changes"),
        "success_criteria": _get_str("success_criteria"),
        "planned_tests": _get_str("planned_tests"),
        "risks": _get_str("risks"),
        "estimated_scope": _get_str("estimated_scope"),
    }

    if not affected_files or any(v is None for v in required.values()):
        return None

    # Optional backward-compatibility fields; defaults are applied by
    # ``ExperimentPlan`` itself, this only normalizes what was present.
    change_kind_raw = _get_str("change_kind")
    change_kind = change_kind_raw.strip().lower() if change_kind_raw else "implementation"

    def _get_list(key: str) -> list[str]:
        value = data.get(key)
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip():
            return [p.strip() for p in re.split(r"[,\n]", value) if p.strip()]
        return []

    required_artifacts = _get_list("required_artifacts")
    acceptance_criteria = _get_list("acceptance_criteria")

    return ExperimentPlan(
        title=required["title"],
        motivation=required["motivation"],
        affected_files=affected_files,
        expected_changes=required["expected_changes"],
        success_criteria=required["success_criteria"],
        planned_tests=required["planned_tests"],
        risks=required["risks"],
        estimated_scope=required["estimated_scope"],
        change_kind=change_kind,
        required_artifacts=required_artifacts,
        acceptance_criteria=acceptance_criteria,
    )
