"""Mistral API coding worker: the third external coding agent next to Codex and
Claude Code.

Mistral has no CLI, so unlike the other workers this one does not spawn a
subprocess: it makes chat-completion calls against a Mistral coding model from
``config/models.json`` (by default ``codestral``) through
``modules.model_registry``, the same OpenAI-compatible interface every registry
model uses. The prompt carries the repository context (the target files named
in the task, rendered by the local worker's helpers), and the model answers
with SEARCH/REPLACE edit blocks; in ``workspace-write`` sandbox the blocks are
converted to a git diff and applied with the local worker's
:func:`apply_and_check` pipeline (apply --check, apply, py_compile rollback),
so a bad patch never lands. A rejected patch gets up to ``MAX_PATCH_ROUNDS``
correction rounds with the error as feedback, like the local worker. In
``read-only`` sandbox nothing is written and the answer is returned as the
final message, like a CLI agent would.

The worker keeps the shared contract of ``modules.coding_worker``: config is
validated before anything starts, output is capped, and the result is a
:class:`WorkerResult` naming the provider. Availability is key-based, not
binary-based: the provider is available when a Mistral coding-role model with
its API key set is configured.
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
    MAX_PATCH_ROUNDS,
    _MAX_FILE_BYTES,
    _SR_CLOSING,
    _SYSTEM_PROMPT,
    _extract_diff,
    _hinted_files,
    _is_search_replace_answer,
    _keywords,
    _render_feedback,
    _render_files,
    apply_and_check,
)
from modules.local_edit_format import edits_to_diff
from modules.local_file_localizer import parse_likely_files

#: This worker's provider name as recorded on every :class:`WorkerResult`.
PROVIDER = "mistral"

#: Only models on Mistral's own API may be drawn for this provider.
_MISTRAL_API_HOST = "api.mistral.ai"

_DEFAULT_MAX_OUTPUT_BYTES: int = 64 * 1024
_DEFAULT_TIMEOUT_SECONDS: int = 120
_DEFAULT_SANDBOX: str = "read-only"
# One call per round; codestral answers a patch in well under this.
_MAX_TOKENS: int = 4096
# Conversion and git-apply steps are local, not model time.
_PATCH_TIMEOUT_SECONDS: float = 30.0


def coding_model(models: list[model_registry.ModelSpec] | None = None) -> model_registry.ModelSpec | None:
    """The first available Mistral coding-role registry model, cheapest first;
    ``None`` when no Mistral model has its API key set. Only ``api.mistral.ai``
    endpoints count, so another vendor's coding model never runs under this
    provider's name."""
    pool = models if models is not None else model_registry.load_models()
    found = [
        spec for spec in model_registry.candidates(pool, role="coding")
        if _MISTRAL_API_HOST in spec.base_url
    ]
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


def _build_prompt(task: str, working_dir: str, feedback: str = "") -> str:
    """System + repository context (target files named in the task) + task.

    Without the file contents a SEARCH/REPLACE answer is guesswork, so the
    files are rendered exactly like the local worker renders them.
    """
    # Files named in the task first; when the task names none, the
    # "Likely files:" hint (#720) supplies the target files instead.
    files = _hinted_files(task, working_dir, parse_likely_files(task))
    files_text = _render_files(files, _keywords(task), (None, _MAX_FILE_BYTES))
    prompt = (
        f"{_SYSTEM_PROMPT}\n\n"
        f"Repository context:\nContents of target files (relevant parts):{files_text or ' none'}\n\n"
        f"{_SR_CLOSING}\n\nTask:\n{task}"
    )
    if feedback:
        prompt += f"\n\n{feedback}"
    return prompt


def _to_patch(text: str, working_dir: str) -> str:
    """Convert the model answer to a git diff, always preferring SEARCH/REPLACE
    (a unified diff remains the escape hatch). The worker's format is not
    affected by ``LOCAL_EDIT_FORMAT`` - that env is for the local model."""
    if _is_search_replace_answer(text):
        return str(edits_to_diff(text, working_dir, _PATCH_TIMEOUT_SECONDS, None))
    diff = _extract_diff(text)
    if diff:
        return diff
    return str(edits_to_diff(text, working_dir, _PATCH_TIMEOUT_SECONDS, None))


class MistralWorker:
    """Runs a coding task with Mistral API calls (one per correction round).

    ``model`` pins a registry model id (default: the cheapest available Mistral
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
                    if not spec.available or _MISTRAL_API_HOST not in spec.base_url:
                        return None
                    return spec
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
        """Answer *task* with Mistral calls; in workspace-write sandbox apply the
        returned edit blocks to *working_dir*, with correction rounds on a
        rejected patch. Never raises for API or patch problems - they are
        reported on the returned :class:`WorkerResult`."""
        started = time.monotonic()
        timestamp = datetime.now(timezone.utc).isoformat()
        spec: model_registry.ModelSpec | None = None
        # Usage is summed across all correction-round calls, so the audit row
        # and any budget accounting see the whole run, not just the last call.
        total_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        def note_usage(raw: dict[str, Any] | None) -> None:
            mapped = _usage_dict(raw)
            if mapped is None:
                return
            for key in total_usage:
                total_usage[key] += mapped.get(key, 0)
            if on_live_usage is not None:
                try:
                    on_live_usage(dict(total_usage))
                except Exception:  # noqa: BLE001 - observers must not break the run
                    pass

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
            if usage is None:
                usage = dict(total_usage) if any(total_usage.values()) else None
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
                    "(MISTRAL_API_KEY) or add a coding-role model on api.mistral.ai "
                    "to config/models.json"
                )
                if experiment_logger is not None:
                    experiment_logger.error(phase, "mistral_model_unavailable", message)
                return finish(False, None, "", message, error=message)

            http_timeout = max(1.0, float(timeout_seconds))

            def transport(url: str, headers: dict[str, str], body: dict[str, Any]) -> httpx.Response:
                return httpx.post(url, headers=headers, json=body, timeout=http_timeout)

            def call(prompt: str) -> model_registry.ChatResult:
                return model_registry.chat(
                    spec, prompt, max_tokens=_MAX_TOKENS,
                    transport=self._transport or transport,
                )

            prompt = _build_prompt(task, working_dir)
            result = call(prompt)
            note_usage(result.usage)

            if not result.success:
                error = "rate_limited" if result.rate_limited else (result.error or "mistral call failed")
                timed_out = result.error in ("ReadTimeout", "ConnectTimeout")
                if experiment_logger is not None:
                    experiment_logger.error(phase, "mistral_call_failed", f"Mistral API call failed: {error}")
                return finish(False, 1, "", error, timed_out=timed_out, error=error)

            content = result.content
            if sandbox == "read-only":
                return finish(True, 0, content, "", final_message=content)

            # Correction rounds: a rejected patch is fed back, like the local
            # worker does, up to MAX_PATCH_ROUNDS answers in total.
            feedback = ""
            for round_no in range(1, MAX_PATCH_ROUNDS + 1):
                try:
                    patch = _to_patch(content, working_dir)
                    apply_and_check(patch, working_dir, _PATCH_TIMEOUT_SECONDS)
                    return finish(True, 0, content, "", final_message=content)
                except Exception as exc:  # noqa: BLE001 - a rejected patch is a result, not a crash
                    outcome = getattr(exc, "outcome", None) or type(exc).__name__
                    reason = f"patch rejected ({outcome}): {exc}"[:1000]
                    if round_no >= MAX_PATCH_ROUNDS:
                        if experiment_logger is not None:
                            experiment_logger.warning(phase, "mistral_patch_rejected", reason)
                        return finish(
                            False, 1, content, reason,
                            final_message=content, error=reason,
                        )
                    feedback = _render_feedback(round_no, reason, content)
                    result = call(_build_prompt(task, working_dir, feedback))
                    note_usage(result.usage)
                    if not result.success:
                        error = "rate_limited" if result.rate_limited else (result.error or "mistral call failed")
                        return finish(
                            False, 1, content, error,
                            final_message=content, error=error,
                        )
                    content = result.content
            return finish(False, 1, content, "patch rounds exhausted", final_message=content, error="patch rounds exhausted")
        finally:
            # No subprocess exists for an API call; the contract still gets its
            # "done" notification so a caller tracking processes can clear up.
            if on_process is not None:
                try:
                    on_process(None)
                except Exception:  # noqa: BLE001 - observers must not break the run
                    pass
