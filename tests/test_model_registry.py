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


def _plain_ok_transport(calls: list):
    """The GitHub Models symptom: every path answers 200 text/plain "OK"."""
    def send(url, headers, body):
        calls.append((url, dict(headers), body["model"]))
        return httpx.Response(200, text="OK", headers={"Content-Type": "text/plain"})
    return send


REPO_MODELS = {
    "github-gpt-4.1-mini": ("https://models.github.ai/inference", "openai/gpt-4.1-mini", "GITHUB_MODELS_TOKEN"),
    "github-gpt-4.1": ("https://models.github.ai/inference", "openai/gpt-4.1", "GITHUB_MODELS_TOKEN"),
    "mistral-small": ("https://api.mistral.ai/v1", "mistral-small-latest", "MISTRAL_API_KEY"),
    "codestral": ("https://codestral.mistral.ai/v1", "codestral-latest", "CODESTRAL_API_KEY"),
    "gemini-3.8-flash": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.8-flash", "GEMINI_API_KEY"),
    "gemini-3.5-flash-lite": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.5-flash-lite", "GEMINI_API_KEY"),
}


class RegistryTests(unittest.TestCase):
    def test_the_repo_file_loads(self):
        ids = [m.id for m in mr.load_models()]
        for model_id in REPO_MODELS:
            self.assertIn(model_id, ids)

    def test_the_free_providers_load_from_config(self):
        models = {m.id: m for m in mr.load_models()}
        for model_id, (base_url, model, api_key_env) in REPO_MODELS.items():
            with self.subTest(model=model_id):
                spec = models[model_id]
                self.assertEqual((spec.base_url, spec.model, spec.api_key_env), (base_url, model, api_key_env))
                self.assertEqual(spec.cost, "free")

    def test_each_new_entry_is_off_without_its_own_key(self):
        models = {m.id: m for m in mr.load_models()}
        for model_id in ("github-gpt-4.1-mini", "mistral-small", "codestral", "gemini-3.8-flash", "gemini-3.5-flash-lite"):
            with self.subTest(model=model_id):
                spec = models[model_id]
                with patch.dict(os.environ, {}, clear=True):
                    self.assertFalse(spec.available)
                    self.assertNotIn(spec, mr.candidates(list(models.values())))
                with patch.dict(os.environ, {spec.api_key_env: "k"}, clear=True):
                    self.assertTrue(spec.available)

    def test_configured_headers_are_sent(self):
        spec = next(m for m in mr.load_models() if m.id == "github-gpt-4.1-mini")
        calls: list = []

        def send(url, headers, body):
            calls.append((url, dict(headers)))
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        with patch.dict(os.environ, {"GITHUB_MODELS_TOKEN": "secret-gh"}, clear=True):
            result = mr.chat(spec, "hi", transport=send)
        self.assertTrue(result.success)
        url, sent = calls[0]
        self.assertEqual(url, "https://models.github.ai/inference/chat/completions")
        self.assertEqual(sent.get("Accept"), "application/vnd.github+json")
        self.assertEqual(sent.get("X-GitHub-Api-Version"), "2022-11-28")
        self.assertEqual(sent.get("Authorization"), "Bearer secret-gh")
        self.assertNotIn("secret-gh", result.error + result.content)

    def test_a_new_provider_is_only_a_config_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.json"
            path.write_text(json.dumps({"models": [
                {"id": "x", "base_url": "https://x/v1/", "model": "m", "cost": "free", "api_key_env": "KEY_X",
                 "headers": {"Accept": "application/vnd.github+json", "X-Test": "1"}},
                {"id": "broken"},
            ]}))
            models = mr.load_models(path)
        self.assertEqual([(m.id, m.base_url) for m in models], [("x", "https://x/v1")])
        self.assertEqual(models[0].headers, (("Accept", "application/vnd.github+json"), ("X-Test", "1")))

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

    def test_a_non_json_200_is_reported_as_non_json_response(self):
        calls: list = []
        with patch.dict(os.environ, {"KEY_A": "secret-a"}, clear=True):
            result = mr.chat(FREE_A, "hi", transport=_plain_ok_transport(calls))
        self.assertFalse(result.success)
        self.assertEqual(result.error, "non_json_response")
        self.assertEqual(calls[0][0], "https://a.example/v1/chat/completions")
        self.assertNotIn("secret-a", result.error)

    def test_a_non_json_200_moves_on_to_the_next_model(self):
        calls: list = []

        def send(url, headers, body):
            calls.append(body["model"])
            if body["model"] == "a":
                return httpx.Response(200, text="OK", headers={"Content-Type": "text/plain"})
            return httpx.Response(200, json={"choices": [{"message": {"content": "from b"}}]})

        with patch.dict(os.environ, KEYS, clear=True):
            result = mr.chat_first("hi", models=[FREE_A, FREE_B], transport=send)
        self.assertTrue(result.success)
        self.assertEqual(result.model_id, "free-b")
        self.assertEqual(calls, ["a", "b"])

    def test_all_non_json_200_is_not_available_and_names_the_error(self):
        with patch.dict(os.environ, KEYS, clear=True):
            result = mr.chat_first("hi", models=[FREE_A, FREE_B], transport=_plain_ok_transport([]))
        self.assertFalse(result.success)
        self.assertTrue(result.error.startswith("not_available"))
        self.assertIn("free-a:non_json_response", result.error)
        self.assertIn("free-b:non_json_response", result.error)
        self.assertNotIn("secret", result.error)

    def test_json_with_the_wrong_shape_is_still_bad_response(self):
        def send(url, headers, body):
            return httpx.Response(200, json={"unexpected": True})

        with patch.dict(os.environ, {"KEY_A": "k"}, clear=True):
            result = mr.chat(FREE_A, "hi", transport=send)
        self.assertFalse(result.success)
        self.assertEqual(result.error, "bad_response")


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
