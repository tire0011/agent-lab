"""Small durable throughput history for local llama-server calls."""

from __future__ import annotations

import json
import os
import statistics
from pathlib import Path
from typing import Any

_MAX_SAMPLES = 21


class LocalModelRates:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.getenv("LOCAL_MODEL_RATES_PATH", "state/local-model-rates.json"))

    def _read(self) -> dict[str, list[float]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"prompt_tokens_per_second": [], "generation_tokens_per_second": []}
        if not isinstance(data, dict):
            return {"prompt_tokens_per_second": [], "generation_tokens_per_second": []}
        return {
            key: [float(value) for value in data.get(key, []) if isinstance(value, (int, float)) and value > 0]
            for key in ("prompt_tokens_per_second", "generation_tokens_per_second")
        }

    def medians(self) -> tuple[float | None, float | None]:
        data = self._read()
        prompt = data["prompt_tokens_per_second"]
        generation = data["generation_tokens_per_second"]
        return (statistics.median(prompt) if prompt else None,
                statistics.median(generation) if generation else None)

    def record(self, timings: dict[str, Any] | None) -> None:
        if not timings:
            return
        samples: dict[str, float] = {}
        for key, n_key, ms_key in (
            ("prompt_tokens_per_second", "prompt_n", "prompt_ms"),
            ("generation_tokens_per_second", "predicted_n", "predicted_ms"),
        ):
            count, milliseconds = timings.get(n_key), timings.get(ms_key)
            if isinstance(count, (int, float)) and isinstance(milliseconds, (int, float)) and milliseconds > 0:
                samples[key] = float(count) * 1000.0 / float(milliseconds)
        if not samples:
            return
        data = self._read()
        for key, sample in samples.items():
            data[key] = [*data[key], sample][-_MAX_SAMPLES:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            pass
