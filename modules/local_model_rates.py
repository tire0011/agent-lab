"""Small durable throughput history for local llama-server calls (#642).

Each successful local model call reports llama-server ``timings``; the prompt
and generation rates (tokens/s) derived from them are kept here as a rolling
window per model, and :meth:`LocalModelRates.medians` feeds the prompt time
budget in :mod:`modules.local_coding_worker`.

Where the file lives: ``LOCAL_MODEL_RATES_PATH`` if set, otherwise next to the
Nexus database (``AGENT_DB_PATH``, default ``/var/lib/nexus/agent.db``). It is
deliberately not a cwd-relative ``state/`` path: the worker runs from experiment
worktrees, and the test suite (which points ``AGENT_DB_PATH`` at a temporary
directory) must never read or write production measurements.

Failure behaviour: a missing, unreadable, corrupt or half-written file reads
as "no measurements yet", which leaves prompts untouched; a failed write is
dropped. Writes go to a unique temporary file that is atomically renamed, so a
crash mid-write or two concurrent writers never leave a broken file behind
(concurrent writers can lose one sample, which a rolling median tolerates).
"""

from __future__ import annotations

import json
import logging
import math
import os
import statistics
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENV_RATES_PATH = "LOCAL_MODEL_RATES_PATH"
RATES_FILE_NAME = "local-model-rates.json"
MAX_SAMPLES = 21
_KEYS = ("prompt_tokens_per_second", "generation_tokens_per_second")
_DEFAULT_DB_PATH = "/var/lib/nexus/agent.db"


def default_rates_path() -> Path:
    explicit = (os.environ.get(ENV_RATES_PATH) or "").strip()
    if explicit:
        return Path(explicit)
    db_path = (os.environ.get("AGENT_DB_PATH") or "").strip() or _DEFAULT_DB_PATH
    return Path(db_path).parent / RATES_FILE_NAME


def _valid_rate(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def rates_from_timings(timings: dict[str, Any] | None) -> dict[str, float]:
    """``{key: tokens/s}`` for the parts of llama-server ``timings`` that are usable."""
    if not isinstance(timings, dict):
        return {}
    samples: dict[str, float] = {}
    for key, n_key, ms_key in (
        ("prompt_tokens_per_second", "prompt_n", "prompt_ms"),
        ("generation_tokens_per_second", "predicted_n", "predicted_ms"),
    ):
        count, milliseconds = timings.get(n_key), timings.get(ms_key)
        if _valid_rate(count) and _valid_rate(milliseconds):
            rate = float(count) * 1000.0 / float(milliseconds)
            if _valid_rate(rate):
                samples[key] = rate
    return samples


class LocalModelRates:
    def __init__(self, path: str | Path | None = None, *, model: str | None = None) -> None:
        self.path = Path(path) if path else default_rates_path()
        self.model = str(model or "default")

    def _read_all(self) -> dict[str, dict[str, list[float]]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, dict):
            return {}
        result: dict[str, dict[str, list[float]]] = {}
        for name, entry in models.items():
            if not isinstance(entry, dict):
                continue
            result[str(name)] = {
                key: [float(v) for v in entry.get(key, []) if _valid_rate(v)]
                if isinstance(entry.get(key), list) else []
                for key in _KEYS
            }
        return result

    def _samples(self) -> dict[str, list[float]]:
        return self._read_all().get(self.model) or {key: [] for key in _KEYS}

    def medians(self) -> tuple[float | None, float | None]:
        """``(prompt tokens/s, generation tokens/s)``; ``None`` where nothing is measured."""
        samples = self._samples()
        prompt, generation = samples["prompt_tokens_per_second"], samples["generation_tokens_per_second"]
        return (statistics.median(prompt) if prompt else None,
                statistics.median(generation) if generation else None)

    def record(self, timings: dict[str, Any] | None) -> bool:
        """Add one call's rates; ``True`` if something was stored. Never raises."""
        try:
            samples = rates_from_timings(timings)
            if not samples:
                return False
            models = self._read_all()
            entry = models.get(self.model) or {key: [] for key in _KEYS}
            for key, sample in samples.items():
                entry[key] = [*entry.get(key, []), sample][-MAX_SAMPLES:]
            models[self.model] = entry
            self._write({"models": models})
            return True
        except Exception:  # measuring must never fail a model call
            logger.warning("could not record local model rates in %s", self.path, exc_info=True)
            return False

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp",
                                         dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(data, indent=2) + "\n")
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
