"""#569: the local coding worker fits its prompt into the model's context, or hands over."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from modules.coding_worker import WorkerResult
from modules.experiment_handover import HANDOVER_HEADING
from modules.experiment_service import ExperimentService
from modules.experiment_service.prompts import _build_direct_implementation_prompt
from modules.llama_cpp_client import LlamaCppClient
from modules.local_coding_worker import (
    CONTEXT_TOO_SMALL,
    LocalCodingWorker,
    split_handover,
)

_PATCH = "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-original\n+patched\n"


class _FakeServer:
    """A llama-server stand-in: /props, /tokenize and chat completion."""

    def __init__(self, *, n_ctx: int | None = 8192, tokenize: bool = True, response: str = _PATCH):
        self.n_ctx = n_ctx
        self.tokenize = tokenize
        self.response = response
        self.props_calls = 0
        self.complete_calls: list[dict] = []

    async def context_size(self) -> int:
        self.props_calls += 1
        if self.n_ctx is None:
            raise httpx.ConnectError("props down")
        return self.n_ctx

    async def count_tokens(self, text: str) -> int:
        if not self.tokenize:
            raise httpx.ConnectError("tokenize down")
        return self.tokens(text)

    @staticmethod
    def tokens(text: str) -> int:
        # Deterministic tokenizer: one token per 4 bytes, rounded up.
        return -(-len(text.encode()) // 4)

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 0, **_kw) -> str:
        self.complete_calls.append({"prompt": prompt, "system": system, "max_tokens": max_tokens})
        return self.response


class _Worker:
    def __init__(self, provider: str, calls: list[str]):
        self.provider = provider
        self.calls = calls

    def run(self, *, working_dir: str, task: str, sandbox: str, **_kwargs):
        self.calls.append(self.provider)
        Path(working_dir, "value.txt").write_text("changed\n")
        return WorkerResult(True, 0, "", "", 1, False, False, False, None, None,
                            None, task, working_dir, sandbox, None, self.provider)


def _task(goal: str, handover: str = "") -> str:
    return _build_direct_implementation_prompt(goal + (f"\n\n{HANDOVER_HEADING}\n\n{handover}" if handover else ""))


class Issue569LocalContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        (self.repo / "value.txt").write_text("original\n")
        # Every line names the goal's keyword, so no excerpt can drop it.
        (self.repo / "huge.py").write_text("".join(f"value_marker = {i}\n" for i in range(20000)))
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, server: _FakeServer, task: str) -> tuple[LocalCodingWorker, WorkerResult]:
        worker = LocalCodingWorker(client=server)
        return worker, worker.run(task=task, working_dir=str(self.repo), sandbox="workspace-write")

    def test_handover_is_split_from_the_prompt_template(self):
        essential, handover = split_handover(_task("Edit value.txt", "found: X"))
        self.assertTrue(handover.startswith(HANDOVER_HEADING))
        self.assertIn("found: X", handover)
        self.assertNotIn(HANDOVER_HEADING, essential)
        self.assertIn("Edit value.txt", essential)
        self.assertIn("Instructions:", essential)

    def test_large_handover_is_cut_and_request_stays_under_n_ctx(self):
        server = _FakeServer(n_ctx=8192)
        handover = "FIRST-FINDING\n" + "".join(f"finding line {i} about the code\n" for i in range(3000))
        worker, result = self._run(server, _task("Edit value.txt to say patched", handover))

        self.assertTrue(result.success, result.error)
        self.assertEqual(1, len(server.complete_calls))
        call = server.complete_calls[0]
        sent = server.tokens(call["system"]) + server.tokens(call["prompt"]) + call["max_tokens"]
        self.assertLessEqual(sent, 8192)
        self.assertIn("original", call["prompt"])  # the named file came before the handover
        self.assertIn("FIRST-FINDING", call["prompt"])  # cut from the end, not the start
        self.assertNotIn("finding line 2999 ", call["prompt"])
        self.assertIn("handover cut", call["prompt"])
        plan = worker.last_plan
        self.assertEqual((8192, "server", "tokenize"), (plan.n_ctx, plan.n_ctx_source, plan.token_source))
        self.assertTrue(plan.handover_cut)
        self.assertEqual(1, server.props_calls)  # read once per run

    def test_named_files_that_cannot_fit_end_with_context_too_small_without_model_call(self):
        server = _FakeServer(n_ctx=8192)
        _worker, result = self._run(server, _task("Change `value_marker` in huge.py", "some finding"))
        self.assertFalse(result.success)
        self.assertTrue((result.error or "").startswith(CONTEXT_TOO_SMALL), result.error)
        self.assertIn("huge.py", result.error)
        self.assertEqual([], server.complete_calls)

    def test_large_named_file_is_cut_to_its_relevant_part(self):
        content = "".join(f"filler_{i} = {i}\n" for i in range(3000))
        content += "def target_function():\n    return 1\n"
        content += "".join(f"tail_{i} = {i}\n" for i in range(3000))
        (self.repo / "big.py").write_text(content)
        server = _FakeServer(n_ctx=8192, response="no diff")
        _worker, _result = self._run(server, _task("Fix `target_function` in big.py"))
        prompt = server.complete_calls[0]["prompt"]
        self.assertIn("def target_function", prompt)
        self.assertNotIn("filler_10 =", prompt)

    def test_context_too_small_hands_the_experiment_to_an_external_agent_at_once(self):
        server = _FakeServer(n_ctx=8192)
        calls: list[str] = []
        service = ExperimentService(
            db_path=str(self.root / "agent.db"), repo_root=str(self.repo),
            worktrees_root=str(self.root / "worktrees"),
            local_worker_factory=lambda _p: LocalCodingWorker(client=server),
            worker_factory=lambda _p: _Worker("codex", calls),
            test_command=["/bin/true"], max_repair_iterations=0,
            experiment_log_dir=str(self.root / "logs"),
        )
        try:
            exp = service.create_experiment(
                "Change `value_marker` in huge.py", complexity="small",
                source_type="issue", source_id="569-too-small",
            )
            result = service.run_experiment(exp.id)
        finally:
            service.close()
        self.assertEqual([], server.complete_calls)
        self.assertEqual(["codex"], calls)
        self.assertEqual("codex", result.implementation_provider)
        self.assertEqual("needs_review", result.status)

    def test_props_failure_uses_local_model_context_then_default(self):
        with mock.patch.dict(os.environ, {"LOCAL_MODEL_CONTEXT": "16384"}):
            worker, result = self._run(_FakeServer(n_ctx=None), _task("Edit value.txt"))
        self.assertTrue(result.success, result.error)
        self.assertEqual((16384, "LOCAL_MODEL_CONTEXT"), (worker.last_plan.n_ctx, worker.last_plan.n_ctx_source))

        (self.repo / "value.txt").write_text("original\n")
        env = {k: v for k, v in os.environ.items() if k != "LOCAL_MODEL_CONTEXT"}
        with mock.patch.dict(os.environ, env, clear=True):
            worker, result = self._run(_FakeServer(n_ctx=None), _task("Edit value.txt"))
        self.assertTrue(result.success, result.error)
        self.assertEqual((8192, "default"), (worker.last_plan.n_ctx, worker.last_plan.n_ctx_source))

    def test_tokenize_failure_falls_back_to_safe_estimate(self):
        server = _FakeServer(n_ctx=8192, tokenize=False)
        handover = "".join(f"finding line {i}\n" for i in range(3000))
        worker, result = self._run(server, _task("Edit value.txt", handover))
        self.assertTrue(result.success, result.error)
        self.assertEqual("estimate", worker.last_plan.token_source)
        call = server.complete_calls[0]
        sent = server.tokens(call["system"]) + server.tokens(call["prompt"]) + call["max_tokens"]
        self.assertLessEqual(sent, 8192)

    def test_client_reads_n_ctx_from_props_and_counts_with_tokenize(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/props":
                return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 16384}})
            if request.url.path == "/tokenize":
                return httpx.Response(200, json={"tokens": [1, 2, 3]})
            return httpx.Response(404)

        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)

        client = LlamaCppClient(base_url="http://llama.test/v1")
        with mock.patch("modules.llama_cpp_client.httpx.AsyncClient", side_effect=factory):
            self.assertEqual(16384, asyncio.run(client.context_size()))
            self.assertEqual(3, asyncio.run(client.count_tokens("abc")))


if __name__ == "__main__":
    unittest.main()
