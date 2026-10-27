"""Bounded, advisory-only reflection after a terminal experiment."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from modules.llama_cpp_client import LlamaCppClient, extract_json, json_schema_response_format
from modules.run_store import DEFAULT_DB_PATH, init_db

MAX_REFLECTION_TOKENS = 384
MAX_SUGGESTIONS = 3

_SYSTEM = """You review a completed software experiment to improve future experiments.
Return JSON only. Suggest zero to three concrete improvements. Do not propose or perform
actions. If nothing is useful, return {\"suggestions\": []}."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "maxItems": MAX_SUGGESTIONS,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "expected_benefit": {"type": "string"},
                    "cost": {"type": "string"},
                    "risk": {"type": "string"},
                    "measurement": {"type": "string"},
                },
                "required": ["title", "expected_benefit", "cost", "risk", "measurement"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["suggestions"],
    "additionalProperties": False,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _title_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _usage_total(*values: Any) -> dict[str, int]:
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for value in values:
        if not isinstance(value, dict):
            continue
        for key in totals:
            raw = value.get(key)
            if isinstance(raw, (int, float)):
                totals[key] += int(raw)
    return totals


def compact_experiment_summary(experiment: Any) -> dict[str, Any]:
    """Return only the bounded evidence requested by the reflection prompt."""
    changed_files = []
    for line in (experiment.git_status or "").splitlines()[:50]:
        path = line[3:].strip() if len(line) > 3 else line.strip()
        if path:
            changed_files.append(path[:300])
    phases = {
        "research_ms": experiment.research_runtime_ms,
        "selection_ms": experiment.selection_runtime_ms,
        "reformat_retry_ms": experiment.reformat_retry_runtime_ms,
        "policy_replan_ms": experiment.policy_replan_runtime_ms,
        "implementation_ms": experiment.implementation_runtime_ms,
    }
    return {
        "goal": experiment.goal[:2000],
        "plan": experiment.plan,
        "changed_files": changed_files,
        "phase_timings": phases,
        "token_usage": _usage_total(
            experiment.research_usage, experiment.selection_usage,
            experiment.reformat_retry_usage, experiment.policy_replan_usage,
            experiment.implementation_usage,
        ),
        "test_result": {"exit_code": experiment.test_exit_code, "status": experiment.status},
        "error": (experiment.error or "")[:2000] or None,
    }


class ExperimentReflectionService:
    """Generate and persist suggestions; never executes or files them."""

    def __init__(
        self,
        *,
        db_path: str = DEFAULT_DB_PATH,
        llm_client: Any | None = None,
        open_issue_titles: Callable[[], Iterable[str]] | None = None,
        max_tokens: int = MAX_REFLECTION_TOKENS,
    ) -> None:
        self._conn = init_db(db_path)
        self._conn.row_factory = sqlite3.Row
        self._client = llm_client or LlamaCppClient()
        self._open_issue_titles = open_issue_titles or (lambda: ())
        self._max_tokens = min(MAX_REFLECTION_TOKENS, max(64, int(max_tokens)))

    def close(self) -> None:
        self._conn.close()

    async def reflect(self, experiment: Any) -> list[dict[str, str]]:
        existing = self._conn.execute(
            "SELECT suggestions_json FROM experiment_reflections WHERE experiment_id=?",
            (experiment.id,),
        ).fetchone()
        if existing:
            return json.loads(existing[0])

        summary = compact_experiment_summary(experiment)
        prompt = "Completed experiment summary:\n" + json.dumps(summary, separators=(",", ":"), default=str)
        raw: str | None = None
        error: str | None = None
        suggestions: list[dict[str, str]] = []
        try:
            raw = await self._client.complete(
                prompt=prompt,
                system=_SYSTEM,
                temperature=0.0,
                max_tokens=self._max_tokens,
                response_format=json_schema_response_format("experiment_reflection", _SCHEMA),
            )
            parsed = (
                {"suggestions": []}
                if raw.strip().casefold().rstrip(".") == "no useful improvement found"
                else extract_json(raw)
            )
            candidates = parsed.get("suggestions")
            if not isinstance(candidates, list):
                raise ValueError("reflection suggestions must be a list")
            open_titles = {_title_key(str(title)) for title in self._open_issue_titles()}
            for candidate in candidates:
                if len(suggestions) >= MAX_SUGGESTIONS:
                    break
                if not isinstance(candidate, dict):
                    continue
                item = {key: str(candidate.get(key) or "").strip() for key in
                        ("title", "expected_benefit", "cost", "risk", "measurement")}
                if not all(item.values()) or _title_key(item["title"]) in open_titles:
                    continue
                suggestions.append(item)
        except Exception as exc:  # best effort: experiment status is already terminal
            error = f"{type(exc).__name__}: {exc}"[:2000]

        with self._conn:
            self._conn.execute(
                """INSERT OR IGNORE INTO experiment_reflections
                   (experiment_id,created_at,model,summary_json,suggestions_json,raw_response,error)
                   VALUES (?,?,?,?,?,?,?)""",
                (experiment.id, _now(), getattr(self._client, "model", None),
                 json.dumps(summary, separators=(",", ":"), default=str),
                 json.dumps(suggestions, separators=(",", ":")), raw, error),
            )
        if error:
            raise RuntimeError(error)
        return suggestions

    def reflect_sync(self, experiment: Any) -> list[dict[str, str]]:
        """Synchronous adapter for the experiment/mailbox worker thread."""
        return asyncio.run(self.reflect(experiment))
