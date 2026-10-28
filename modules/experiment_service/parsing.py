from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from modules.experiment_evaluation import drop_git_internal

from .models import ExperimentPlan

# The checkout this code runs from; experiment worktrees share its layout.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])


# JSON Schema for the structured plan, passed to Codex via
# ``CodexWorker.run(output_schema=...)`` for both the selection phase and
# the reformat retry. Codex's own ``--output-schema`` support constrains
# the *shape* of the model's final response; the result is still fully
# re-validated with parse_plan()/plan_violates_policy()/validate_plan()
# below (no semantic values are trusted or guessed just because the shape
# matched).
PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "motivation": {"type": "string"},
        "affected_files": {"type": "array", "items": {"type": "string"}},
        "expected_changes": {"type": "string"},
        "success_criteria": {"type": "string"},
        "planned_tests": {"type": "string"},
        "risks": {"type": "string"},
        "estimated_scope": {"type": "string"},
        "change_kind": {
            "type": "string",
            "enum": ["implementation", "tests_only", "documentation", "analysis"],
        },
        "required_artifacts": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "title",
        "motivation",
        "affected_files",
        "expected_changes",
        "success_criteria",
        "planned_tests",
        "risks",
        "estimated_scope",
        "change_kind",
        "required_artifacts",
        "acceptance_criteria",
    ],
    "additionalProperties": False,
}



# ---------------------------------------------------------------------------

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
    """Scan *text* and return the exact substrings of every top-level
    balanced ``{...}`` object, tracking string/escape state so that braces
    inside JSON string values (e.g. in a ``"risks"`` free-text field) never
    throw off the matching. Only top-level objects are returned (nested
    objects are part of their parent's span, not reported separately).
    """
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
    """Tolerantly extract exactly one JSON object from *raw_text*.

    Accepts, in order of preference:
      1. The whole trimmed text being pure JSON.
      2. Exactly one JSON object inside a ```json fenced code block.
      3. Exactly one top-level balanced ``{...}`` object anywhere in the
         text (braces inside JSON string values are correctly ignored via
         proper string/escape tracking, unlike a naive first/last-brace
         slice).

    Returns ``None`` — never guesses or merges — if zero or more than one
    top-level JSON object is found, or if the found object does not parse.
    No values are invented; this only locates and decodes exactly one
    object if unambiguously present.
    """
    if not raw_text or not raw_text.strip():
        return None

    text = raw_text.strip()

    # 1) Whole text is pure JSON.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # 2) Exactly one fenced ```json ... ``` block containing an object.
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

    # 3) Exactly one top-level balanced {...} object anywhere in the text.
    candidates = _find_balanced_json_objects(text)
    if len(candidates) != 1:
        return None  # zero or ambiguous multiple objects: reject, don't guess

    try:
        parsed = json.loads(candidates[0])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_plan(raw_text: str | None) -> ExperimentPlan | None:
    """Best-effort parse of the selection phase's structured output.

    Prefers a single, unambiguous JSON object (see
    ``extract_single_json_object``). Falls back to a simple
    ``Field: value`` line format only when no JSON object is present at
    all. Returns ``None`` if the fields cannot be reliably extracted —
    callers must treat that as "reject, do not guess".
    """
    if not raw_text or not raw_text.strip():
        return None

    text = raw_text.strip()
    data = extract_single_json_object(text)

    if data is None:
        # Fallback: "Field: value" lines. Only used when the text contains
        # no JSON object candidates at all (extract_single_json_object
        # returns None both for "no object" and "ambiguous multiple
        # objects" — in the ambiguous case this fallback will also very
        # likely fail to find clean field lines, which is the desired
        # conservative behavior).
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
    # Paths inside git's own directory can never be committed (#403).
    affected_files, _ = drop_git_internal(affected_files, _REPO_ROOT)

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

    # New, optional-for-backward-compatibility fields: absent in any plan
    # produced before this vocabulary existed, or in a raw model response
    # that fell back to the "Field: value" text format above. Defaults are
    # applied by ``ExperimentPlan`` itself; this only normalizes whatever
    # was actually present.
    change_kind_raw = _get_str("change_kind")
    change_kind = change_kind_raw.strip().lower() if change_kind_raw else "implementation"

    def _get_list(key: str) -> list[str]:
        value = data.get(key)
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip():
            return [p.strip() for p in re.split(r"[,\n]", value) if p.strip()]
        return []

    required_artifacts, _ = drop_git_internal(_get_list("required_artifacts"), _REPO_ROOT)
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
