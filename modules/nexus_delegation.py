"""Bounded LLM delegation tools used by the local Nexus controller.

The local model remains the controller. Delegates are consultants/workers that
return an observation; control always comes back to the local orchestrator.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx

from modules.llama_cpp_client import LlamaCppClient
from modules.run_store import DEFAULT_DB_PATH


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
    ) -> None:
        self.llama = llama_client or LlamaCppClient(timeout=120.0)
        self.codex_service = codex_service
        self.db_path = db_path
        self.repo_root = repo_root
        self._owns_codex = False

    def close(self) -> None:
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
            return await self._codex(task, context)
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
        try:
            raw = await self.llama.complete(prompt=prompt, temperature=0.1, max_tokens=900)
            return DelegationResult("local_worker", True, raw, {})
        except Exception as exc:  # noqa: BLE001
            return DelegationResult("local_worker", False, "", {"error": type(exc).__name__})

    async def _codex(self, task: str, context: str) -> DelegationResult:
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
            created = await asyncio.to_thread(service.create_task, prompt, self.repo_root, "read-only", 120)
            finished = await asyncio.to_thread(service.run_task, created.id)
            content = finished.final_message or finished.error or f"Codex status: {finished.status}"
            return DelegationResult(
                "codex_readonly",
                finished.status == "succeeded",
                content,
                {"task_id": finished.id, "status": finished.status, "runtime_ms": finished.runtime_ms, "usage": finished.usage},
            )
        except Exception as exc:  # noqa: BLE001
            return DelegationResult("codex_readonly", False, "", {"error": type(exc).__name__})

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
                return DelegationResult("azure_openai", False, "", {"error": f"HTTP {response.status_code}"})
            data = response.json()
            return DelegationResult(
                "azure_openai",
                True,
                str(data["choices"][0]["message"]["content"]),
                {"deployment": deployment, "usage": data.get("usage") or {}},
            )
        except Exception as exc:  # noqa: BLE001
            return DelegationResult("azure_openai", False, "", {"error": type(exc).__name__})
