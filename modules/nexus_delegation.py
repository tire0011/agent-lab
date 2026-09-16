"""Bounded LLM delegation tools used by the local Nexus controller.

The local model remains the controller. Delegates are consultants/workers that
return an observation; control always comes back to the local orchestrator.

The ``local_worker`` delegate runs on the same shared local-LLM job path as
every other local-model caller (#167/D-019): it is another role on the one
queue, not a second way to reach llama-server. The external delegates
(``codex_readonly``, ``claude_readonly``, ``azure_openai``) talk to their own
services and are not affected by that rule.

The two CLI coding agents are offered as *named* delegates here: this is the
controller deliberately consulting one of them, so nothing is drawn at random
(that happens for tasks and experiments — see ``modules.coding_provider``).
``claude_readonly`` is only advertised when the Claude CLI is installed.
"""

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

_AZURE_ERROR_DETAIL_LIMIT = 1000


def sanitize_azure_error_detail(raw: str, *, secrets: tuple[str, ...] = ()) -> str:
    """Return bounded Azure error text with known credential values redacted."""
    detail = raw[:_AZURE_ERROR_DETAIL_LIMIT]
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, "[REDACTED]")
    return detail


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
        # Local delegation is queued work, not a private HTTP path: the same
        # shared service the controller and failure analysis use runs it, so a
        # delegate cannot race them at llama-server.
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
        return DelegationResult(provider, False, f"Unknown delegation provider: {provider}", {})

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
        """Consult one named CLI coding agent in read-only mode.

        The task service is shared between both agents; the provider is passed
        per task, so a delegate the controller asked for by name is never
        swapped for the other one.
        """
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
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
        api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
        deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
        if not endpoint or not api_key:
            return DelegationResult("azure_openai", False, "", {"error": "not_configured"})
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-05-01-preview"
        prompt = (
            "You are a specialist consulted by a local Nexus controller. Analyse the "
            "sub-problem and return concise findings and a recommended next action. "
            "You do not control execution and must not claim to have used unavailable tools.\n\n"
            f"SUB-PROBLEM:\n{task[:6000]}\n\nCONTEXT:\n{context[:6000]}"
        )
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    url,
                    headers={"api-key": api_key, "Content-Type": "application/json"},
                    json={"messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 900},
                )
            if response.status_code != 200:
                detail = sanitize_azure_error_detail(response.text, secrets=(api_key,))
                return DelegationResult(
                    "azure_openai",
                    False,
                    "",
                    {
                        "error": f"HTTP {response.status_code}",
                        "http_status": response.status_code,
                        "error_detail": detail,
                        "deployment": deployment,
                    },
                )
            data = response.json()
            return DelegationResult(
                "azure_openai",
                True,
                str(data["choices"][0]["message"]["content"]),
                {"deployment": deployment, "usage": data.get("usage") or {}},
            )
        except Exception as exc:  # noqa: BLE001
            return DelegationResult("azure_openai", False, "", {"error": type(exc).__name__})
