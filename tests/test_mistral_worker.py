"""Unit tests for modules.mistral_worker and its registration in
modules.coding_provider.

No real HTTP call is ever made: the worker takes a fake ``transport``. The
workspace-write cases run against a temporary git repo, like the local-worker
tests do.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from modules import model_registry
from modules.coding_provider import (
    ENV_PROVIDER_ALLOW_LIST,
    ENV_PROVIDER_PIN,
    KNOWN_PROVIDERS,
    allowed_providers,
    build_worker,
    is_available,
    is_provider_overload,
    pinned_provider,
)
from modules.mistral_worker import PROVIDER, MistralWorker, coding_model


def _clean_env(**overrides):
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in (ENV_PROVIDER_PIN, ENV_PROVIDER_ALLOW_LIST, "MISTRAL_API_KEY")
    }
    env.update(overrides)
    return patch.dict(os.environ, env, clear=True)


def _chat_response(content: str, status: int = 200, usage: dict | None = None) -> httpx.Response:
    payload: dict = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        payload["usage"] = usage
    return httpx.Response(status, json=payload)


def _transport(content: str, status: int = 200, usage: dict | None = None):
    def transport(url, headers, body):
        return _chat_response(content, status=status, usage=usage)

    return transport


_SR_BLOCK = (
    "value.txt\n<<<<<<< SEARCH\noriginal\n=======\nchanged\n>>>>>>> REPLACE\n"
)


class RegistrationTests(unittest.TestCase):
    def test_mistral_is_a_known_provider(self):
        self.assertIn(PROVIDER, KNOWN_PROVIDERS)
        with _clean_env():
            self.assertIn(PROVIDER, allowed_providers())

    def test_allow_list_accepts_mistral(self):
        with _clean_env(**{ENV_PROVIDER_ALLOW_LIST: "mistral"}):
            self.assertEqual(allowed_providers(), [PROVIDER])

    def test_pin_accepts_mistral(self):
        with _clean_env(**{ENV_PROVIDER_PIN: "mistral"}):
            self.assertEqual(pinned_provider(), PROVIDER)

    def test_build_worker_returns_mistral_worker(self):
        worker = build_worker(PROVIDER)
        self.assertIsInstance(worker, MistralWorker)
        self.assertEqual(worker.provider, PROVIDER)

    def test_availability_is_key_based(self):
        with _clean_env():
            self.assertFalse(is_available(PROVIDER))
        with _clean_env(MISTRAL_API_KEY="test-key"):
            self.assertTrue(is_available(PROVIDER))


class CodingModelTests(unittest.TestCase):
    def test_prefers_coding_role_and_available_key(self):
        specs = [
            model_registry.ModelSpec(
                id="general", base_url="https://example.invalid", model="m",
                cost="free", api_key_env="MISTRAL_API_KEY", roles=("planning",),
            ),
            model_registry.ModelSpec(
                id="coder", base_url="https://example.invalid", model="c",
                cost="free", api_key_env="MISTRAL_API_KEY", roles=("coding",),
            ),
        ]
        with _clean_env(MISTRAL_API_KEY="test-key"):
            self.assertEqual(coding_model(specs).id, "coder")
        with _clean_env():
            self.assertIsNone(coding_model(specs))


class _RepoMixin:
    def _make_repo(self) -> Path:
        root = Path(self.tmp.name)
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "value.txt").write_text("original\n")
        subprocess.run(["git", "add", "value.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
        return repo


class WorkerRunTests(_RepoMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self._make_repo()
        self.key_env = _clean_env(MISTRAL_API_KEY="test-key")
        self.key_env.start()

    def tearDown(self):
        self.key_env.stop()
        self.tmp.cleanup()

    def test_read_only_returns_answer_without_writing(self):
        worker = MistralWorker(transport=_transport("just an answer"))
        result = worker.run("say hi", str(self.repo), sandbox="read-only")
        self.assertTrue(result.success)
        self.assertEqual(result.provider, PROVIDER)
        self.assertEqual(result.final_message, "just an answer")
        self.assertEqual((self.repo / "value.txt").read_text(), "original\n")

    def test_workspace_write_applies_search_replace_block(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        worker = MistralWorker(transport=_transport(_SR_BLOCK, usage=usage))
        result = worker.run("change the value", str(self.repo), sandbox="workspace-write")
        self.assertTrue(result.success, msg=result.stderr)
        self.assertEqual((self.repo / "value.txt").read_text(), "changed\n")
        self.assertEqual(
            result.usage,
            {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    def test_rejected_patch_leaves_tree_untouched(self):
        bad = "value.txt\n<<<<<<< SEARCH\nnot there\n=======\nchanged\n>>>>>>> REPLACE\n"
        worker = MistralWorker(transport=_transport(bad))
        result = worker.run("change the value", str(self.repo), sandbox="workspace-write")
        self.assertFalse(result.success)
        self.assertIn("patch rejected", result.error or "")
        self.assertEqual((self.repo / "value.txt").read_text(), "original\n")

    def test_unknown_pinned_model_fails_honestly(self):
        worker = MistralWorker(model="does-not-exist", transport=_transport(_SR_BLOCK))
        result = worker.run("task", str(self.repo))
        self.assertFalse(result.success)
        self.assertIn("no available Mistral coding model", result.error or "")

    def test_invalid_config_is_rejected_before_any_call(self):
        worker = MistralWorker(transport=_transport(_SR_BLOCK))
        result = worker.run("task", "relative/path")
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)

    def test_rate_limit_is_reported_and_counts_as_overload(self):
        worker = MistralWorker(transport=_transport("", status=429))
        result = worker.run("task", str(self.repo))
        self.assertFalse(result.success)
        self.assertEqual(result.error, "rate_limited")
        self.assertTrue(is_provider_overload(result.error))

    def test_read_timeout_sets_timed_out(self):
        def raising_transport(url, headers, body):
            raise httpx.ReadTimeout("too slow", request=None)

        worker = MistralWorker(transport=raising_transport)
        result = worker.run("task", str(self.repo))
        self.assertFalse(result.success)
        self.assertTrue(result.timed_out)

    def test_on_process_gets_done_notification(self):
        seen: list = []
        worker = MistralWorker(transport=_transport("ok"))
        worker.run("task", str(self.repo), on_process=seen.append)
        self.assertEqual(seen, [None])


if __name__ == "__main__":
    unittest.main()
