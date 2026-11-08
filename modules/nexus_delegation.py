"""Bounded LLM delegation tools for the local Nexus controller: delegates are consultants
that return an observation, control always comes back to the local orchestrator. The
``local_worker`` delegate runs on the shared local-LLM job queue like every other local
caller (#167/D-019) - one queue, not a second path to llama-server. The CLI coding agents
are offered as *named* delegates (the controller deliberately consults one, nothing is
drawn at random); ``claude_readonly`` is advertised only when the Claude CLI is installed."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx

from modules.coding_provider import PROVIDER_CLAUDE, PROVIDER_CODEX, is_available
from modules.llama_cpp_client import LlamaCppClient
from modules.local_llm_jobs import (
    ROLE_LOCAL_WORKER,
    LocalLlmJobError,
    LocalLlmJobService,
    acquire_local_llm_job_service,
    release_local_llm_job_service,
)
from modules.run_store import DEFAULT_DB_PATH


_AZURE_ERROR_DETAIL_LIMIT = 2000


def _sanitize_azure_error_detail(response: httpx.Response, api_key: str) -> str:
    """Return bounded Azure failure details without leaking the configured key."""
    try:
        detail = response.text.strip()
    except Exception:  # noqa: BLE001
        detail = ""
    if api_key:
        detail = detail.replace(api_key, "[REDACTED]")
    return detail[:_AZURE_ERROR_DETAIL_LIMIT]


@dataclass(slots=True)
class DelegationResult:
    provider: str
    success: bool
    content: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "success": self.success,
            "content": self.content[:4000],
            "metadata": self.metadata,
        }


FREE_TIER = "free_tier"
MODEL_PREFIX = "model:"


def _registry_catalog() -> list[dict[str, Any]]:
    """Registry models as delegates: ``free_tier`` plus one entry per model (#552)."""
    from modules import model_registry

    available = [m for m in model_registry.load_models() if m.available]
    entries: list[dict[str, Any]] = []
    if any(m.cost == "free" for m in available):
        entries.append({
            "provider": FREE_TIER,
            "description": "Ask the free external models in turn (rate-limited, no budget cost); "
                           "use before paid providers.",
            "writes_repo": False,
            "cost": "free",
        })
    for m in available:
        entries.append({
            "provider": MODEL_PREFIX + m.id,
            "description": m.description or m.model,
            "writes_repo": False,
            "cost": m.cost,
        })
    return entries


class NexusDelegationService:
    """Expose available LLMs as tools rather than as top-level control flow."""

    def __init__(
        self,
        *,
        llama_client: LlamaCppClient | None = None,
        codex_service: Any | None = None,
        db_path: str = DEFAULT_DB_PATH,
        repo_root: str = "/agent",
        llm_job_service: LocalLlmJobService | None = None,
    ) -> None:
        # Local delegation is queued work, not a private HTTP path: the same shared service the
        # controller and failure analysis use runs it, so a delegate cannot race them at llama-server.
        self.llm_jobs = llm_job_service or acquire_local_llm_job_service(
            db_path=db_path, llama_client=llama_client
        )
        self._owns_llm_jobs = llm_job_service is None
        self.llama = llama_client or self.llm_jobs.llama
        self.codex_service = codex_service
        self.db_path = db_path
        self.repo_root = repo_root
        self._owns_codex = False

    def close(self) -> None:
        if self._owns_llm_jobs:
            release_local_llm_job_service(self.llm_jobs)
        if self._owns_codex and self.codex_service is not None:
            close = getattr(self.codex_service, "close", None)
            if close:
                close()

    def catalog(self) -> list[dict[str, Any]]:
        providers = [
            {
                "provider": "local_worker",
                "description": "Ask the local model to analyse or solve a bounded sub-problem; control returns to Nexus afterwards.",
                "writes_repo": False,
                "cost": "local",
            },
            {
                "provider": "codex_readonly",
                "description": "Delegate difficult repository analysis/review to Codex in read-only mode; use new_experiment for implementation changes.",
                "writes_repo": False,
                "cost": "subscription/external",
            },
        ]
        if is_available(PROVIDER_CLAUDE):
            providers.append(
                {
                    "provider": "claude_readonly",
                    "description": "Delegate difficult repository analysis/review to Claude Code in read-only mode; use new_experiment for implementation changes.",
                    "writes_repo": False,
                    "cost": "subscription/external",
                }
            )
        if os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_API_KEY"):
            providers.append(
                {
                    "provider": "azure_openai",
                    "description": "Ask configured Azure OpenAI for difficult reasoning/research when local reasoning is insufficient.",
                    "writes_repo": False,
                    "cost": "paid",
                }
            )
        providers.extend(_registry_catalog())
        return providers

    async def delegate(self, provider: str, task: str, *, context: str = "") -> DelegationResult:
        provider = provider.strip().lower()
        if provider == "local_worker":
            return await self._local(task, context)
        if provider == "codex_readonly":
            return await self._cli_agent(PROVIDER_CODEX, "codex_readonly", task, context)
        if provider == "claude_readonly":
            return await self._cli_agent(PROVIDER_CLAUDE, "claude_readonly", task, context)
        if provider == "azure_openai":
            return await self._azure(task, context)
        if provider == FREE_TIER or provider.startswith(MODEL_PREFIX):
            return await self._registry(provider, task, context)
        return DelegationResult(provider, False, f"Unknown delegation provider: {provider}", {})

    async def _registry(self, provider: str, task: str, context: str) -> DelegationResult:
        """A model from config/models.json (#552); ``free_tier`` tries the free ones in order."""
        from modules import model_registry

        prompt = (
            "You are a bounded worker consulted by the Nexus controller. "
            "Solve or analyse the sub-problem below. Return concise findings and a "
            f"recommended next step.\n\nSUB-PROBLEM:\n{task[:12000]}\n\nCONTEXT:\n{context[:6000]}"
            if context else task[:16000]
        )
        if provider == FREE_TIER:
            result = await asyncio.to_thread(model_registry.chat_first, prompt, max_cost="free")
        else:
            wanted = provider[len(MODEL_PREFIX):]
            spec = next((m for m in model_registry.load_models() if m.id == wanted and m.available), None)
            if spec is None:
                return DelegationResult(provider, False, "", {"error": "not_configured"})
            result = await asyncio.to_thread(model_registry.chat, spec, prompt)
        metadata = {"model": result.model_id, "cost": result.cost}
        if not result.success:
            metadata["error"] = result.error
        return DelegationResult(provider, result.success, result.content, metadata)

    async def _local(self, task: str, context: str) -> DelegationResult:
        prompt = (
            "You are a bounded worker consulted by the Nexus controller. "
            "Solve or analyse the sub-problem below. Do not claim to have used tools. "
            "Return concise findings and a recommended next step.\n\n"
            f"SUB-PROBLEM:\n{task[:6000]}\n\nCONTEXT:\n{context[:6000]}"
        )
        job_id = self.llm_jobs.submit(
            prompt, temperature=0.1, max_tokens=900, role=ROLE_LOCAL_WORKER
        )
        try:
            raw = await self.llm_jobs.wait(job_id)
            return DelegationResult("local_worker", True, raw, {"job_id": job_id})
        except LocalLlmJobError as exc:
            return DelegationResult(
                "local_worker",
                False,
                "",
                {"job_id": job_id, "error": "LocalLlmJobError", "error_kind": exc.kind},
            )
        except Exception as exc:  # noqa: BLE001
            return DelegationResult(
                "local_worker", False, "", {"job_id": job_id, "error": type(exc).__name__}
            )

    async def _cli_agent(
        self, provider: str, delegate: str, task: str, context: str
    ) -> DelegationResult:
        """Consult one named CLI coding agent in read-only mode: the shared task service runs both
        agents and the provider is passed per task, so a delegate asked for by name is never
        swapped for the other one."""
        try:
            service = self.codex_service
            if service is None:
                from modules.codex_task_service import CodexTaskService
                service = CodexTaskService(db_path=self.db_path)
                self.codex_service = service
                self._owns_codex = True
            prompt = (
                "You are a read-only specialist consulted by the Nexus controller. "
                "Inspect the repository as needed, but do not modify files. Analyse the "
                "sub-problem and return findings, evidence and a recommended next step.\n\n"
                f"SUB-PROBLEM:\n{task[:6000]}\n\nKNOWN CONTEXT:\n{context[:6000]}"
            )
            created = await asyncio.to_thread(
                service.create_task, prompt, self.repo_root, "read-only", 120, provider
            )
            finished = await asyncio.to_thread(service.run_task, created.id)
            content = (
                finished.final_message
                or finished.error
                or f"{provider} status: {finished.status}"
            )
            return DelegationResult(
                delegate,
                finished.status == "succeeded",
                content,
                {
                    "task_id": finished.id,
                    "status": finished.status,
                    "runtime_ms": finished.runtime_ms,
                    "usage": finished.usage,
                    "provider": finished.provider,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return DelegationResult(delegate, False, "", {"error": type(exc).__name__})

    async def _azure(self, task: str, context: str) -> DelegationResult:
        prompt = (
            "You are a specialist consulted by a local Nexus controller. Analyse the "
            "sub-problem and return concise findings and a recommended next action. "
            "You do not control execution and must not claim to have used unavailable tools.\n\n"
            f"SUB-PROBLEM:\n{task[:6000]}\n\nCONTEXT:\n{context[:6000]}"
        )
        return await self._azure_chat(prompt, max_tokens=900, timeout=60.0)

    async def azure_chat(self, prompt: str, *, max_tokens: int) -> DelegationResult:
        """Send *prompt* to Azure as is - no wrapper, no truncation: used when Azure drives the
        controller itself (#337), where unlike a delegated sub-problem the prompt must arrive whole."""
        return await self._azure_chat(prompt, max_tokens=max_tokens, timeout=120.0)

    async def _azure_chat(self, prompt: str, *, max_tokens: int, timeout: float) -> DelegationResult:
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
        api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
        deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
        if not endpoint or not api_key:
            return DelegationResult("azure_openai", False, "", {"error": "not_configured"})
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-05-01-preview"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    url,
                    headers={"api-key": api_key, "Content-Type": "application/json"},
                    json={"messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": max_tokens},
                )
            if response.status_code != 200:
                metadata: dict[str, Any] = {
                    "error": f"HTTP {response.status_code}",
                    "http_status": response.status_code,
                    "deployment": deployment,
                }
                detail = _sanitize_azure_error_detail(response, api_key)
                if detail:
                    metadata["error_detail"] = detail
                return DelegationResult("azure_openai", False, "", metadata)
            data = response.json()
            return DelegationResult(
                "azure_openai",
                True,
                str(data["choices"][0]["message"]["content"]),
                {"deployment": deployment, "usage": data.get("usage") or {}},
            )
        except Exception as exc:  # noqa: BLE001
            return DelegationResult("azure_openai", False, "", {"error": type(exc).__name__})
