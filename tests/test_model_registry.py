"""One interface for chat models from config/models.json (#552)."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from modules import model_registry as mr
from modules.model_registry import ModelSpec

FREE_A = ModelSpec("free-a", "https://a.example/v1", "a", "free", api_key_env="KEY_A")
FREE_B = ModelSpec("free-b", "https://b.example/v1", "b", "free", api_key_env="KEY_B")
PAID = ModelSpec("paid", "https://p.example/v1", "p", "paid", api_key_env="KEY_P")
KEYS = {"KEY_A": "secret-a", "KEY_B": "secret-b", "KEY_P": "secret-p"}


def _transport(answers: dict[str, int], calls: list):
    def send(url, headers, body):
        calls.append((url, headers.get("Authorization"), body["model"]))
        status = answers.get(body["model"], 200)
        payload = {"choices": [{"message": {"content": f"answer from {body['model']}"}}]}
        return httpx.Response(status, json=payload)
    return send


class RegistryTests(unittest.TestCase):
    def test_the_repo_file_loads(self):
        ids = [m.id for m in mr.load_models()]
        self.assertIn("github-gpt-4.1-mini", ids)

    def test_a_new_provider_is_only_a_config_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.json"
            path.write_text(json.dumps({"models": [
                {"id": "x", "base_url": "https://x/v1/", "model": "m", "cost": "free", "api_key_env": "KEY_X"},
                {"id": "broken"},
            ]}))
            models = mr.load_models(path)
        self.assertEqual([(m.id, m.base_url) for m in models], [("x", "https://x/v1")])

    def test_without_its_key_a_model_is_off(self):
        with patch.dict(os.environ, {"KEY_A": "k"}, clear=True):
            self.assertEqual([m.id for m in mr.candidates([PAID, FREE_A, FREE_B])], ["free-a"])

    def test_cheapest_first_and_max_cost(self):
        with patch.dict(os.environ, KEYS, clear=True):
            self.assertEqual([m.id for m in mr.candidates([PAID, FREE_A])], ["free-a", "paid"])
            self.assertEqual([m.id for m in mr.candidates([PAID, FREE_A], max_cost="free")], ["free-a"])

    def test_a_rate_limit_moves_on_to_the_next_model(self):
        calls: list = []
        with patch.dict(os.environ, KEYS, clear=True):
            result = mr.chat_first("hi", models=[FREE_A, FREE_B], transport=_transport({"a": 429}, calls))
        self.assertTrue(result.success)
        self.assertEqual(result.model_id, "free-b")
        self.assertEqual([c[2] for c in calls], ["a", "b"])
        self.assertEqual(calls[0][1], "Bearer secret-a")

    def test_all_limited_is_not_available(self):
        with patch.dict(os.environ, KEYS, clear=True):
            result = mr.chat_first("hi", models=[FREE_A, FREE_B], transport=_transport({"a": 429, "b": 429}, []))
        self.assertFalse(result.success)
        self.assertTrue(result.error.startswith("not_available"))
        self.assertNotIn("secret", result.error)


class DelegationTests(unittest.TestCase):
    def test_free_tier_is_offered_and_answers_with_its_cost(self):
        from modules import nexus_delegation

        with patch.dict(os.environ, {"GITHUB_MODELS_TOKEN": "t"}, clear=True):
            providers = {p["provider"]: p["cost"] for p in nexus_delegation._registry_catalog()}
            self.assertEqual(providers.get("free_tier"), "free")
            self.assertIn("model:github-gpt-4.1-mini", providers)
            service = nexus_delegation.NexusDelegationService.__new__(nexus_delegation.NexusDelegationService)
            with patch.object(mr, "chat_first", return_value=mr.ChatResult("github-gpt-4.1-mini", "free", True, "ok")):
                result = asyncio.run(service._registry("free_tier", "task", ""))
        self.assertTrue(result.success)
        self.assertEqual(result.metadata["cost"], "free")

    def test_without_keys_nothing_is_offered(self):
        from modules import nexus_delegation

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(nexus_delegation._registry_catalog(), [])


if __name__ == "__main__":
    unittest.main()
