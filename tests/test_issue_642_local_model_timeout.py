"""#642: local calls expose timings, timeouts, and throughput-sized prompts."""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from modules.experiment_handover import HANDOVER_HEADING
from modules.llama_cpp_client import LlamaCppClient, LlamaCppClientError
from modules.local_coding_worker import LocalCodingWorker, build_prompt


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, _phase, event, _message, **fields):
        self.events.append((event, fields))

    warning = info

    def all(self, name: str) -> list[dict]:
        return [fields for event, fields in self.events if event == name]


class _Server:
    timeout = 300.0

    def __init__(self, answer: str | Exception, timings: dict | None = None) -> None:
        self.answer = answer
        self.timings_to_set = timings
        self.last_timings = None
        self.calls = 0

    async def context_size(self):
        return 20_000

    async def count_tokens(self, text):
        return max(1, (len(text.encode()) + 3) // 4)

    async def complete(self, *_args, **_kwargs):
        self.calls += 1
        self.last_timings = self.timings_to_set
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Issue642Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "value.txt").write_text("old\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_client_timeout_names_limit_prompt_and_output(self):
        client = LlamaCppClient(base_url="http://llama.test/v1", timeout=300)
        with patch("modules.llama_cpp_client.httpx.AsyncClient") as cls:
            mocked = AsyncMock()
            mocked.__aenter__ = AsyncMock(return_value=mocked)
            mocked.__aexit__ = AsyncMock(return_value=None)
            mocked.post = AsyncMock(side_effect=httpx.ReadTimeout(""))
            cls.return_value = mocked
            with self.assertRaisesRegex(
                LlamaCppClientError,
                r"timed out after 300s \(prompt ~\d+ tokens, max_tokens 77\)",
            ):
                asyncio.run(client.chat_completion(
                    [{"role": "user", "content": "a reasonably sized prompt"}], max_tokens=77,
                ))

    def test_client_keeps_llama_server_timings(self):
        timings = {"prompt_n": 100, "prompt_ms": 5000, "predicted_n": 20,
                   "predicted_ms": 4000, "ignored": 1}
        response = MagicMock(status_code=200)
        response.json.return_value = {"choices": [], "timings": timings}
        with patch("modules.llama_cpp_client.httpx.AsyncClient") as cls:
            mocked = AsyncMock()
            mocked.__aenter__ = AsyncMock(return_value=mocked)
            mocked.__aexit__ = AsyncMock(return_value=None)
            mocked.post = AsyncMock(return_value=response)
            cls.return_value = mocked
            client = LlamaCppClient(base_url="http://llama.test/v1")
            asyncio.run(client.chat_completion([{"role": "user", "content": "hi"}]))
        self.assertEqual({key: timings[key] for key in timings if key != "ignored"}, client.last_timings)

    def test_patch_timeout_is_counted_and_stops_cleanly(self):
        server = _Server(LlamaCppClientError(
            "local model call timed out after 300s (prompt ~100 tokens, max_tokens 1024)"
        ))
        log = _Log()
        result = LocalCodingWorker(
            client=server, max_tokens=1024, rates_path=Path(self.temp.name) / "rates.json"
        ).run(task="Edit value.txt", working_dir=str(self.repo), sandbox="workspace-write",
              experiment_logger=log)
        self.assertFalse(result.success)
        self.assertIn("timed out after 300s", result.error)
        self.assertEqual("timeout", log.all("local_model_call")[0]["outcome"])
        self.assertEqual("model_timeout", log.all("local_patch_round")[0]["outcome"])
        self.assertEqual(1, log.all("local_patch_round")[0]["round"])

    def test_timings_are_forwarded_to_model_call_event(self):
        timings = {"prompt_n": 80, "prompt_ms": 4000,
                   "predicted_n": 10, "predicted_ms": 2000}
        log = _Log()
        LocalCodingWorker(
            client=_Server("not a diff", timings), max_tokens=1024,
            rates_path=Path(self.temp.name) / "rates.json",
        ).run(task="Edit value.txt", working_dir=str(self.repo), sandbox="workspace-write",
              experiment_logger=log)
        event = log.all("local_model_call")[0]
        self.assertEqual(timings, event["timings"])
        self.assertEqual(20.0, event["prompt_tokens_per_second"])
        self.assertEqual(5.0, event["generation_tokens_per_second"])

    def test_measured_rates_cut_handover_but_absent_rates_do_not(self):
        server = _Server("unused")
        handover = "\n".join(f"finding {number}" for number in range(4000))
        task = f"Edit value.txt\n\n{HANDOVER_HEADING}\n{handover}"
        unrestricted = asyncio.run(build_prompt(
            task, str(self.repo), server, max_output_tokens=1024,
        ))
        fitted = asyncio.run(build_prompt(
            task, str(self.repo), server, max_output_tokens=1024, rates=(20.0, 20.0),
        ))
        self.assertIsNone(unrestricted.time_budget_tokens)
        self.assertFalse(unrestricted.time_budget_limited)
        self.assertEqual(3776, fitted.time_budget_tokens)
        self.assertTrue(fitted.time_budget_limited)
        self.assertLess(fitted.handover_tokens_kept, unrestricted.handover_tokens_kept)
        self.assertLessEqual(fitted.prompt_tokens, fitted.time_budget_tokens)

    def test_time_limited_prompt_skips_planning_and_spends_one_call_on_patch(self):
        rates = Path(self.temp.name) / "rates.json"
        rates.write_text(
            '{"prompt_tokens_per_second":[20],"generation_tokens_per_second":[20]}',
            encoding="utf-8",
        )
        server = _Server("not a diff")
        log = _Log()
        LocalCodingWorker(
            client=server, max_tokens=1024, split_tasks=True, rates_path=rates,
        ).run(task="Edit value.txt", working_dir=str(self.repo), sandbox="workspace-write",
              experiment_logger=log)
        self.assertEqual("patch", log.all("local_model_call")[0]["call_type"])
        self.assertTrue(log.all("local_task_split")[0]["skipped"])


if __name__ == "__main__":
    unittest.main()
