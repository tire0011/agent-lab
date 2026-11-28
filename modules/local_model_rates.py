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

Speculative decoding (#669): when llama-server runs with a draft model its
``timings`` carry ``draft_n`` and ``draft_n_accepted``;
:func:`draft_from_timings` turns them into an acceptance rate, which
:meth:`LocalModelRates.record` keeps in its own rolling window
(:meth:`LocalModelRates.draft_acceptance`). Without them nothing changes.

Sample age and re-probe state (#772): every stored sample batch gets a UTC
timestamp in the parallel list ``sampled_at`` (:meth:`LocalModelRates.newest_sample_age`).
Admission refusals are counted (:meth:`LocalModelRates.record_refusal`,
:meth:`LocalModelRates.consecutive_refusals`) and the last rate re-probe is kept
(:meth:`LocalModelRates.record_probe_attempt`); a refused call never measures
anything, so without them a slow median could never change again. Any stored
sample resets the refusal counter. Files written before #772 have none of these
keys; their samples count as of unknown, i.e. old, age.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENV_RATES_PATH = "LOCAL_MODEL_RATES_PATH"
RATES_FILE_NAME = "local-model-rates.json"
MAX_SAMPLES = 21
_KEYS = ("prompt_tokens_per_second", "generation_tokens_per_second")
_ESTIMATED = {key: f"estimated_{key}" for key in _KEYS}
# Rolling share of drafted tokens the main model accepted (#669). Recorded with
# the measured rates; never used for the time budget.
_DRAFT_KEY = "draft_acceptance_rate"
_ALL_KEYS = (*_KEYS, *_ESTIMATED.values(), _DRAFT_KEY)
_DEFAULT_DB_PATH = "/var/lib/nexus/agent.db"
# #772: per-model state next to the rolling samples.
_SAMPLED_AT = "sampled_at"
_REFUSALS = "consecutive_refusals"
_LAST_PROBE = "last_probe_at"


def default_rates_path() -> Path:
    explicit = (os.environ.get(ENV_RATES_PATH) or "").strip()
    if explicit:
        return Path(explicit)
    db_path = (os.environ.get("AGENT_DB_PATH") or "").strip() or _DEFAULT_DB_PATH
    return Path(db_path).parent / RATES_FILE_NAME


def _valid_sample(key: str, value: Any) -> bool:
    if key == _DRAFT_KEY:  # an acceptance rate of 0 is a real measurement
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and 0 <= value <= 1)
    return _valid_rate(value)


def _valid_rate(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def draft_from_timings(timings: dict[str, Any] | None) -> dict[str, float | int]:
    """Speculative-decoding counters from llama-server ``timings`` (#669).

    ``{"draft_n", "draft_n_accepted", "draft_acceptance_rate"}`` when the server
    reports drafted tokens (only with ``--model-draft``); ``{}`` when they are
    missing, malformed or nothing was drafted.
    """
    if not isinstance(timings, dict):
        return {}
    drafted, accepted = timings.get("draft_n"), timings.get("draft_n_accepted")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
               and v >= 0 for v in (drafted, accepted)):
        return {}
    if drafted <= 0 or accepted > drafted:
        return {}
    return {"draft_n": int(drafted), "draft_n_accepted": int(accepted),
            "draft_acceptance_rate": float(accepted) / float(drafted)}


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

    def _read_all(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for name, entry in models.items():
            if not isinstance(entry, dict):
                continue
            parsed: dict[str, Any] = {
                key: [float(v) for v in entry.get(key, []) if _valid_sample(key, v)]
                if isinstance(entry.get(key), list) else []
                for key in _ALL_KEYS
            }
            stamps = entry.get(_SAMPLED_AT)
            parsed[_SAMPLED_AT] = ([v for v in stamps if _parse_time(v) is not None]
                                   if isinstance(stamps, list) else [])
            refusals = entry.get(_REFUSALS)
            parsed[_REFUSALS] = (refusals if isinstance(refusals, int)
                                 and not isinstance(refusals, bool) and refusals > 0 else 0)
            probe = entry.get(_LAST_PROBE)
            parsed[_LAST_PROBE] = probe if _parse_time(probe) is not None else None
            result[str(name)] = parsed
        return result

    def _empty(self) -> dict[str, Any]:
        return {**{key: [] for key in _ALL_KEYS}, _SAMPLED_AT: [], _REFUSALS: 0, _LAST_PROBE: None}

    def _samples(self) -> dict[str, Any]:
        return self._read_all().get(self.model) or self._empty()

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

    def draft_acceptance(self) -> float | None:
        """Median draft acceptance rate (0-1); ``None`` without speculative decoding."""
        values = self._samples().get(_DRAFT_KEY) or []
        return statistics.median(values) if values else None

    def latest_rate(self, key: str) -> float | None:
        """The newest stored sample of *key*; ``None`` when nothing is stored.

        *key* is one of ``prompt_tokens_per_second`` / ``generation_tokens_per_second``.
        Like :meth:`medians`, the measured samples win and the timeout estimates
        are only used for a rate that was never measured; "latest" is the last
        element of that list (the most recent recorded sample).
        """
        if key not in _KEYS:
            raise ValueError(f"unknown rate {key!r}; expected one of {', '.join(_KEYS)}")
        samples = self._samples()
        values = samples.get(key) or samples.get(_ESTIMATED[key]) or []
        return float(values[-1]) if values else None

    def generation_measured(self) -> bool:
        """Whether the generation rate comes from real timings, not a guess."""
        return bool(self._samples().get("generation_tokens_per_second"))

    def record(self, timings: dict[str, Any] | None, *, replace: bool = False) -> bool:
        """Add one call's rates; ``True`` if something was stored. Never raises.

        With speculative decoding the call's draft acceptance rate is stored
        with them (#669); it alone does not count as a stored measurement.

        *replace* (#772, the rate re-probe) first drops the stored prompt and
        generation samples, measured and estimated: the probe only runs when
        that window is suspect (stale or refusing every call), and a single
        sample could never move its median. Real calls rebuild the window.
        """
        try:
            samples: dict[str, float] = rates_from_timings(timings)
            if not samples:
                return False
            draft = draft_from_timings(timings)
            if draft:
                samples[_DRAFT_KEY] = float(draft["draft_acceptance_rate"])
            self._append(samples, replace=replace)
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

    # Sample age, refusals and re-probes (#772)

    def newest_sample_age(self, now: datetime | None = None) -> float | None:
        """Seconds since the newest stored rate sample.

        ``None`` without any rate sample; ``math.inf`` when samples exist but
        carry no timestamp (a file written before #772), so they count as old.
        """
        samples = self._samples()
        if not any(samples.get(key) for key in (*_KEYS, *_ESTIMATED.values())):
            return None
        stamps = [t for t in (_parse_time(v) for v in samples.get(_SAMPLED_AT) or []) if t]
        if not stamps:
            return math.inf
        return max(0.0, (_utc(now) - max(stamps)).total_seconds())

    def consecutive_refusals(self) -> int:
        """Admission refusals since the last stored sample."""
        return int(self._samples().get(_REFUSALS) or 0)

    def record_refusal(self) -> int:
        """Count one admission refusal; the new count (0 if it could not be stored)."""
        try:
            def change(entry: dict[str, Any]) -> None:
                entry[_REFUSALS] = int(entry.get(_REFUSALS) or 0) + 1
            return int(self._update(change).get(_REFUSALS) or 0)
        except Exception:
            logger.warning("could not record a local admission refusal in %s", self.path,
                           exc_info=True)
            return 0

    def seconds_since_probe(self, now: datetime | None = None) -> float | None:
        """Seconds since the last rate re-probe started; ``None`` if there was none."""
        probe = _parse_time(self._samples().get(_LAST_PROBE))
        if probe is None:
            return None
        return max(0.0, (_utc(now) - probe).total_seconds())

    def record_probe_attempt(self, now: datetime | None = None) -> bool:
        """Remember that a re-probe starts now (the cooldown runs from here)."""
        try:
            stamp = _utc(now).isoformat()

            def change(entry: dict[str, Any]) -> None:
                entry[_LAST_PROBE] = stamp
            self._update(change)
            return True
        except Exception:
            logger.warning("could not record a local rate probe in %s", self.path, exc_info=True)
            return False

    def _update(self, change: Any) -> dict[str, Any]:
        models = self._read_all()
        entry = models.get(self.model) or self._empty()
        change(entry)
        models[self.model] = entry
        self._write({"models": models})
        return entry

    def _append(self, samples: dict[str, float], now: datetime | None = None, *,
                replace: bool = False) -> None:
        stamp = _utc(now).isoformat()

        def change(entry: dict[str, Any]) -> None:
            if replace:
                for key in (*_KEYS, *_ESTIMATED.values(), _SAMPLED_AT):
                    entry[key] = []
            for key, sample in samples.items():
                entry[key] = [*entry.get(key, []), sample][-MAX_SAMPLES:]
            entry[_SAMPLED_AT] = [*entry.get(_SAMPLED_AT, []), stamp][-MAX_SAMPLES:]
            entry[_REFUSALS] = 0  # a real call measured something
        self._update(change)

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
