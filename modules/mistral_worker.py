"""Mistral API coding worker: the third external coding agent next to Codex and
Claude Code.

Mistral has no CLI, so unlike the other workers this one does not spawn a
subprocess: it makes one chat-completion call per task against a coding-role
model from ``config/models.json`` (by default ``codestral``) through
``modules.model_registry``, the same OpenAI-compatible interface every registry
model uses. The model answers with SEARCH/REPLACE edit blocks (the format the
local worker already validates); in ``workspace-write`` sandbox the blocks are
converted to a git diff and applied with the local worker's
:func:`apply_and_check` pipeline (apply --check, apply, py_compile rollback),
so a bad patch never lands. In ``read-only`` sandbox nothing is written and the
answer is returned as the final message, like a CLI agent would.

The worker keeps the shared contract of ``modules.coding_worker``: config is
validated before anything starts, output is capped, and the result is a
:class:`WorkerResult` naming the provider. Availability is key-based, not
binary-based: the provider is available when a coding-role model with its API
key set is configured.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx

from modules import model_registry
from modules.coding_worker import (
    WorkerConfigError,
    WorkerResult,
    truncate_output,
    validate_worker_config,
)
from modules.local_coding_worker import (
    _SR_CLOSING,
    _SYSTEM_PROMPT,
    _response_patch,
    apply_and_check,
)

#: This worker's provider name as recorded on every :class:`WorkerResult`.
PROVIDER = "mistral"

_DEFAULT_MAX_OUTPUT_BYTES: int = 64 * 1024
_DEFAULT_TIMEOUT_SECONDS: int = 120
_DEFAULT_SANDBOX: str = "read-only"
# One call per task; codestral answers a patch in well under this.
_MAX_TOKENS: int = 4096
# Conversion and git-apply steps are local, not model time.
_PATCH_TIMEOUT_SECONDS: float = 30.0


def coding_model(models: list[model_registry.ModelSpec] | None = None) -> model_registry.ModelSpec | None:
    """The first available coding-role registry model, cheapest first; ``None``
    when no model has its API key set. This is the provider's availability."""
    pool = models if models is not None else model_registry.load_models()
    found = model_registry.candidates(pool, role="coding")
    return found[0] if found else None


def _usage_dict(raw: dict[str, Any] | None) -> dict[str, int] | None:
    """Map the API's ``prompt_tokens``/``completion_tokens`` onto the shared
    ``input_tokens``/``output_tokens`` keys of :class:`WorkerResult`."""
    if not raw:
        return None
    try:
        return {
            "input_tokens": int(raw.get("prompt_tokens") or 0),
            "output_tokens": int(raw.get("completion_tokens") or 0),
            "total_tokens": int(raw.get("total_tokens") or 0),
        }
    except (TypeError, ValueError):
        return None


class MistralWorker:
    """Runs a coding task with one Mistral API call.

    ``model`` pins a registry model id (default: the cheapest available
    coding-role model); ``max_output_bytes`` caps stdout/stderr; ``db_path``
    ``None`` skips the ``runs`` audit row; ``transport`` overrides the HTTP call
    (tests). ``output_schema``, ``skip_git_repo_check``, ``stall_seconds`` and
    ``on_process`` are accepted for interface compatibility with the CLI workers
    - there is no subprocess to watch or kill, so the stall watchdog and process
    tracking do not apply.
    """

    #: Provider name recorded on every result this worker produces.
    provider = PROVIDER

    def __init__(
        self,
        *,
        db_path: str | None = None,
        model: str | None = None,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        transport: model_registry.Transport | None = None,
    ) -> None:
        self._db_path = db_path
        self.model = model
        self.max_output_bytes = max_output_bytes
        self._transport = transport

    def _select_model(self) -> model_registry.ModelSpec | None:
        pool = model_registry.load_models()
        if self.model:
            for spec in pool:
                if spec.id == self.model:
                    return spec if spec.available else None
            return None
        return coding_model(pool)

    def _audit(self, result: WorkerResult, spec: model_registry.ModelSpec | None, timestamp: str) -> None:
        if self._db_path is None:
            return
        try:
            from modules.run_store import init_db, insert_run
            conn = init_db(self._db_path)
            usage = result.usage or {}
            insert_run(
                conn,
                timestamp=timestamp,
                provider=PROVIDER,
                model=spec.model if spec is not None else "mistral",
                input_description=result.task[:500],
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                runtime_ms=result.runtime_ms,
                estimated_cost=None,
                actual_cost=None,
                cost_status="unknown",
                success=result.success,
                error=result.error,
            )
            conn.close()
        except Exception:
            # Audit failure must never surface to the caller.
            pass

    def run(
        self,
        task: str,
        working_dir: str,
        sandbox: str = _DEFAULT_SANDBOX,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        output_schema: dict | None = None,
        skip_git_repo_check: bool = False,
        experiment_logger: Any | None = None,
        phase: str = "mistral",
        stall_seconds: float | None = None,
        on_process: Any | None = None,
        on_live_usage: Any | None = None,
    ) -> WorkerResult:
        """Answer *task* with one Mistral call; in workspace-write sandbox apply
        the returned edit blocks to *working_dir*. Never raises for API or patch
        problems - they are reported on the returned :class:`WorkerResult`."""
        started = time.monotonic()
        timestamp = datetime.now(timezone.utc).isoformat()
        spec: model_registry.ModelSpec | None = None

        def finish(
            success: bool,
            exit_code: int | None,
            stdout: str,
            stderr: str,
            *,
            timed_out: bool = False,
            final_message: str | None = None,
            usage: dict[str, int] | None = None,
            error: str | None = None,
        ) -> WorkerResult:
            out, out_truncated = truncate_output(stdout, self.max_output_bytes)
            err, err_truncated = truncate_output(stderr, self.max_output_bytes)
            result = WorkerResult(
                success=success,
                exit_code=exit_code,
                stdout=out,
                stderr=err,
                runtime_ms=int((time.monotonic() - started) * 1000),
                timed_out=timed_out,
                stdout_truncated=out_truncated,
                stderr_truncated=err_truncated,
                thread_id=None,
                final_message=final_message,
                usage=usage,
                task=task,
                working_dir=working_dir,
                sandbox=sandbox,
                error=error,
                provider=PROVIDER,
            )
            self._audit(result, spec, timestamp)
            return result

        try:
            try:
                validate_worker_config(task, working_dir, sandbox)
            except WorkerConfigError as exc:
                if experiment_logger is not None:
                    experiment_logger.error(
                        phase, "mistral_call_invalid_config",
                        f"Mistral call rejected before starting: {exc}",
                    )
                return finish(False, None, "", str(exc), error=str(exc))

            spec = self._select_model()
            if spec is None:
                message = (
                    "no available Mistral coding model: set the model's api_key_env "
                    "(MISTRAL_API_KEY) or add a coding-role model to config/models.json"
                )
                if experiment_logger is not None:
                    experiment_logger.error(phase, "mistral_model_unavailable", message)
                return finish(False, None, "", message, error=message)

            prompt = f"{_SYSTEM_PROMPT}\n{_SR_CLOSING}\n\nTask:\n{task}"
            http_timeout = max(1.0, float(timeout_seconds))

            def transport(url: str, headers: dict[str, str], body: dict[str, Any]) -> httpx.Response:
                return httpx.post(url, headers=headers, json=body, timeout=http_timeout)

            result = model_registry.chat(
                spec,
                prompt,
                max_tokens=_MAX_TOKENS,
                transport=self._transport or transport,
            )
            usage = _usage_dict(result.usage)
            if on_live_usage is not None and usage is not None:
                try:
                    on_live_usage(usage)
                except Exception:  # noqa: BLE001 - observers must not break the run
                    pass

            if not result.success:
                timed_out = result.error in ("ReadTimeout", "ConnectTimeout")
                if experiment_logger is not None:
                    experiment_logger.error(
                        phase, "mistral_call_failed", f"Mistral API call failed: {result.error}"
                    )
                return finish(
                    False, 1, "", result.error or "mistral call failed",
                    timed_out=timed_out, usage=usage, error=result.error,
                )

            content = result.content
            if sandbox == "read-only":
                return finish(True, 0, content, "", final_message=content, usage=usage)

            try:
                patch = str(_response_patch(content, working_dir, _PATCH_TIMEOUT_SECONDS, None))
                apply_and_check(patch, working_dir, _PATCH_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001 - a rejected patch is a result, not a crash
                outcome = getattr(exc, "outcome", None) or type(exc).__name__
                reason = f"patch rejected ({outcome}): {exc}"[:1000]
                if experiment_logger is not None:
                    experiment_logger.warning(phase, "mistral_patch_rejected", reason)
                return finish(
                    False, 1, content, reason,
                    final_message=content, usage=usage, error=reason,
                )
            return finish(True, 0, content, "", final_message=content, usage=usage)
        finally:
            # No subprocess exists for an API call; the contract still gets its
            # "done" notification so a caller tracking processes can clear up.
            if on_process is not None:
                try:
                    on_process(None)
                except Exception:  # noqa: BLE001 - observers must not break the run
                    pass
