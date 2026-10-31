"""A hung local model call fails in bounded time and hands over to the fallback (#236)."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from modules import local_llm_jobs
from modules.central_logging import get_task_logger, read_log_entries
from modules.local_llm_jobs import (
    ERROR_KIND_TIMEOUT,
    ROLE_CONTROLLER,
    STATUS_FAILED,
    LocalLlmJobError,
    LocalLlmJobService,
    local_llm_watchdog_seconds,
)
from modules.nexus_delegation import DelegationResult
from modules.task_orchestrator import ACTION_ANSWER, ACTION_MISSING_CAPABILITY, NexusTaskOrchestrator
from tests.test_task_orchestrator import _Registry

_WATCHDOG_ENV = "NEXUS_LOCAL_LLM_JOB_WATCHDOG_SECONDS"
_ANSWER = (
    '{{"type":"action","action":"answer","experiment_id":null,'
    '"message":"{message}","reason":"finished"}}'
)


class _HangingClient:
    """A local server that never answers; records that the call was torn down."""

    model = "test-local-model"

    def __init__(self):
        self.cancelled = False

    async def _hang(self):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None):
        del prompt, temperature, max_tokens, response_format
        await self._hang()

    async def chat_completion(self, messages, **kwargs):
        del messages, kwargs
        await self._hang()


class _DelayedClient:
    model = "test-local-model"

    def __init__(self, reply: str, delay: float):
        self.reply = reply
        self.delay = delay

    async def complete(self, *, prompt, temperature, max_tokens, response_format=None):
        del prompt, temperature, max_tokens, response_format
        await asyncio.sleep(self.delay)
        return self.reply


def _delegation(*results: DelegationResult) -> MagicMock:
    delegation = MagicMock()
    delegation.catalog.return_value = [
        {"provider": result.provider, "description": "external", "writes_repo": False, "cost": "paid"}
        for result in results
    ]
    delegation.delegate = AsyncMock(side_effect=list(results))
    return delegation


@patch.dict(os.environ, {"NEXUS_CONTROLLER_PROVIDER": "local", _WATCHDOG_ENV: "0.05"})
@patch.object(local_llm_jobs, "_MIN_WATCHDOG_SECONDS", 0.0)
class ControllerTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "state.db")

    def _orchestrator(self, client, delegation) -> NexusTaskOrchestrator:
        orchestrator = NexusTaskOrchestrator(
            operations_registry=_Registry(self.db_path, self.tmp.name),
            experiment_service=MagicMock(),
            llama_client=client,
            delegation_service=delegation,
        )
        self.addCleanup(orchestrator.close)
        return orchestrator

    def test_hung_call_is_recorded_as_timeout_and_the_fallback_answers(self):
        client = _HangingClient()
        delegation = _delegation(
            DelegationResult(
                provider="azure_openai",
                success=True,
                content=_ANSWER.format(message="done externally"),
                metadata={},
            )
        )
        orchestrator = self._orchestrator(client, delegation)
        task_log = get_task_logger("issue-236", log_dir=self.tmp.name)

        result = orchestrator.run("Answer after a hang", task_logger=task_log)

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(result.message, "done externally")
        delegation.delegate.assert_called_once()
        self.assertEqual(delegation.delegate.call_args.args[0], "azure_openai")
        self.assertTrue(client.cancelled, "the hung request must be torn down, not left running")

        job = orchestrator.llm_jobs.recent(role=ROLE_CONTROLLER)[0]
        self.assertEqual(job.status, STATUS_FAILED)
        self.assertEqual(job.error_kind, ERROR_KIND_TIMEOUT)
        self.assertIn("exceeded the 0s watchdog", job.error)

        events = [
            entry
            for entry in read_log_entries(task_log.path)
            if entry.get("event") == "controller_local_inference_failed"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["error_kind"], ERROR_KIND_TIMEOUT)
        self.assertEqual(events[0]["job_id"], job.id)

    def test_call_within_the_timeout_is_used_and_nothing_falls_back(self):
        os.environ[_WATCHDOG_ENV] = "10"
        delegation = _delegation()
        orchestrator = self._orchestrator(
            _DelayedClient(_ANSWER.format(message="done locally"), delay=0.1), delegation
        )

        result = orchestrator.run("Answer slowly")

        self.assertEqual(result.action, ACTION_ANSWER)
        self.assertEqual(result.message, "done locally")
        delegation.delegate.assert_not_called()

    def test_fallback_failure_after_a_timeout_names_both_causes(self):
        delegation = _delegation(
            DelegationResult(
                provider="azure_openai",
                success=False,
                content="",
                metadata={"error": "HTTP 429 quota exceeded"},
            )
        )
        orchestrator = self._orchestrator(_HangingClient(), delegation)

        result = orchestrator.run("Answer after a hang")

        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)
        for text in (result.reason, result.message):
            self.assertIn("exceeded the 0s watchdog", text)
            self.assertIn("azure_openai: HTTP 429 quota exceeded", text)

    def test_timeout_without_any_external_provider_is_reported(self):
        orchestrator = self._orchestrator(_HangingClient(), _delegation())

        result = orchestrator.run("Answer after a hang")

        self.assertEqual(result.action, ACTION_MISSING_CAPABILITY)
        self.assertIn("exceeded the 0s watchdog", result.message)
        self.assertIn("no external provider is configured", result.message)


class WatchdogServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_logprob_calls_are_bounded_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = LocalLlmJobService(
                db_path=str(Path(tmp) / "jobs.db"), llama_client=_HangingClient(), poll_interval=0.01
            )
            service.watchdog_seconds = 0.05
            try:
                job_id = service.submit("hello", top_logprobs=5)
                with self.assertRaises(LocalLlmJobError) as raised:
                    await service.wait(job_id)
            finally:
                service.close()
        self.assertEqual(raised.exception.kind, ERROR_KIND_TIMEOUT)


class WatchdogConfigurationTests(unittest.TestCase):
    def _watchdog(self, value: str | None) -> float:
        env = {key: item for key, item in os.environ.items() if key != _WATCHDOG_ENV}
        if value is not None:
            env[_WATCHDOG_ENV] = value
        with patch.dict(os.environ, env, clear=True):
            return local_llm_watchdog_seconds()

    def test_default_is_well_below_the_old_hour(self):
        self.assertEqual(self._watchdog(None), 900.0)
        self.assertEqual(self._watchdog(""), 900.0)

    def test_operator_override_changes_the_timeout(self):
        self.assertEqual(self._watchdog("120"), 120.0)

    def test_override_is_clamped(self):
        self.assertEqual(self._watchdog("1"), 60.0)
        self.assertEqual(self._watchdog("99999"), 7200.0)

    def test_invalid_override_falls_back_to_the_default(self):
        self.assertEqual(self._watchdog("soon"), 900.0)

    def test_override_reaches_the_service_and_the_real_client(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {_WATCHDOG_ENV: "120"}):
            service = LocalLlmJobService(db_path=str(Path(tmp) / "jobs.db"))
            try:
                self.assertEqual(service.watchdog_seconds, 120.0)
                self.assertEqual(service.llama.timeout, 120.0)
                self.assertEqual(service._stuck_after_seconds, 240.0)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
