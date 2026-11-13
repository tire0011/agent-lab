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

Measured vs. estimated (#667): :meth:`LocalModelRates.record` stores rates
measured from llama-server timings (also the partial timings of a stopped
call); :meth:`LocalModelRates.record_timeout` stores guesses under separate
``estimated_*`` keys. :meth:`LocalModelRates.medians` uses the measured samples
of a rate and falls back to the estimates only when that rate was never
measured, and :meth:`LocalModelRates.generation_measured` tells whether the
generation rate is a real measurement. Admission also uses conservative
estimates: that is what prevents a zero-token timeout from immediately being
repeated. Files written before #667 have no ``estimated_*`` keys.

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
_ESTIMATED = {key: f"estimated_{key}" for key in _KEYS}
_ALL_KEYS = (*_KEYS, *_ESTIMATED.values())
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
                for key in _ALL_KEYS
            }
        return result

    def _samples(self) -> dict[str, list[float]]:
        return self._read_all().get(self.model) or {key: [] for key in _ALL_KEYS}

    def medians(self) -> tuple[float | None, float | None]:
        """``(prompt tokens/s, generation tokens/s)``; ``None`` where nothing is known.

        Per rate the measured samples, else the timeout estimates.
        """
        samples = self._samples()
        result = []
        for key in _KEYS:
            values = samples.get(key) or samples.get(_ESTIMATED[key]) or []
            result.append(statistics.median(values) if values else None)
        return result[0], result[1]

    def generation_measured(self) -> bool:
        """Whether the generation rate comes from real timings, not a guess."""
        return bool(self._samples().get("generation_tokens_per_second"))

    def record(self, timings: dict[str, Any] | None) -> bool:
        """Add one call's rates; ``True`` if something was stored. Never raises."""
        try:
            samples = rates_from_timings(timings)
            if not samples:
                return False
            self._append(samples)
            return True
        except Exception:  # measuring must never fail a model call
            logger.warning("could not record local model rates in %s", self.path, exc_info=True)
            return False

    def record_timeout(self, *, prompt_tokens: int, timeout_seconds: float,
                       minimum_output_tokens: int) -> bool:
        """Bootstrap conservative rates when a timed-out call has no timings.

        The guesses are stored as estimates (#667): they size the next prompt
        only for a rate that was never measured, and never count as a measured
        generation rate.

        Conservatively assume prompt ingestion consumed the whole timeout.  In
        the next call, half of the 80%-of-time budget is reserved for a minimal
        useful answer; the other half therefore admits about 40% of the timed-out
        prompt.  The inferred samples use the ordinary rolling window, so real
        llama-server timings replace them naturally.  Like :meth:`record`, this
        never raises.
        """
        try:
            if not (isinstance(prompt_tokens, int) and not isinstance(prompt_tokens, bool)
                    and prompt_tokens > 0):
                return False
            if not (_valid_rate(timeout_seconds)
                    and isinstance(minimum_output_tokens, int)
                    and not isinstance(minimum_output_tokens, bool)
                    and minimum_output_tokens > 0):
                return False
            half_budget_seconds = float(timeout_seconds) * 0.8 * 0.5
            self._append({
                _ESTIMATED["prompt_tokens_per_second"]: prompt_tokens / float(timeout_seconds),
                _ESTIMATED["generation_tokens_per_second"]: minimum_output_tokens / half_budget_seconds,
            })
            return True
        except Exception:
            logger.warning("could not record local model timeout rates in %s", self.path,
                           exc_info=True)
            return False

    def record_prompt_stop(self, timings: dict[str, Any] | None, *, prompt_tokens: int,
                           timeout_seconds: float, minimum_output_tokens: int) -> bool:
        """Atomically store a TTFT prompt bound and generation fallback.

        Keeping both samples in one replacement means interruption cannot leave
        a measured prompt rate without the estimate needed to admit/refuse the
        next call. Repeating the operation is safe: the bounded rolling median
        receives the same observations again.
        """
        try:
            measured = rates_from_timings(timings)
            if "prompt_tokens_per_second" not in measured:
                return False
            if not (isinstance(prompt_tokens, int) and not isinstance(prompt_tokens, bool)
                    and prompt_tokens > 0 and _valid_rate(timeout_seconds)
                    and isinstance(minimum_output_tokens, int)
                    and not isinstance(minimum_output_tokens, bool)
                    and minimum_output_tokens > 0):
                return False
            half_budget_seconds = float(timeout_seconds) * 0.8 * 0.5
            self._append({
                **measured,
                _ESTIMATED["prompt_tokens_per_second"]: prompt_tokens / float(timeout_seconds),
                _ESTIMATED["generation_tokens_per_second"]:
                    minimum_output_tokens / half_budget_seconds,
            })
            return True
        except Exception:
            logger.warning("could not record local model prompt stop rates in %s", self.path,
                           exc_info=True)
            return False

    def _append(self, samples: dict[str, float]) -> None:
        models = self._read_all()
        entry = models.get(self.model) or {key: [] for key in _ALL_KEYS}
        for key, sample in samples.items():
            entry[key] = [*entry.get(key, []), sample][-MAX_SAMPLES:]
        models[self.model] = entry
        self._write({"models": models})

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
