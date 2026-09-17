"""Every declared JSON schema must be sent where a server will look for it.

Nexus sent `{"type": "json_schema", "schema": {...}}` - the schema one level
above where both llama-server and Azure OpenAI read it. Neither found it, so
every schema was silently ignored: the tool resolver, constrained on paper to
four `status` values, answered with a fifth inside Markdown fences, and its
otherwise correct selection was discarded (#218).

Probed against the real llama-server on 2026-09-17: the old shape returned the
forbidden value, this shape returned the constrained one.

The failure mode is what makes this worth a test. An ignored schema is
invisible - it looks exactly like a model that happens to comply - so nothing
fails until a model does not, and then it fails somewhere else entirely.
"""

from __future__ import annotations

import unittest

from modules.failure_analysis_service import _TOOL_DECISION_RESPONSE_FORMAT
from modules.llama_cpp_client import json_schema_response_format
from modules.nexus_tool_router import TOOL_RESOLVER_RESPONSE_FORMAT
from modules.task_orchestrator import _ORCHESTRATOR_RESPONSE_FORMAT

# Every response format Nexus sends to a model, by the role that sends it.
ALL_RESPONSE_FORMATS = {
    "tool resolver": TOOL_RESOLVER_RESPONSE_FORMAT,
    "controller": _ORCHESTRATOR_RESPONSE_FORMAT,
    "failure analysis": _TOOL_DECISION_RESPONSE_FORMAT,
}


class JsonSchemaWireShapeTests(unittest.TestCase):
    def test_every_schema_is_nested_where_servers_read_it(self):
        for role, response_format in ALL_RESPONSE_FORMATS.items():
            with self.subTest(role=role):
                self.assertEqual(response_format["type"], "json_schema")
                envelope = response_format["json_schema"]
                self.assertIsInstance(envelope.get("schema"), dict)
                self.assertTrue(envelope.get("name"))
                self.assertTrue(envelope.get("strict"))

    def test_no_schema_sits_at_the_top_level_where_it_is_ignored(self):
        # The exact regression: a top-level "schema" key is the shape neither
        # server reads, and it fails silently rather than loudly.
        for role, response_format in ALL_RESPONSE_FORMATS.items():
            with self.subTest(role=role):
                self.assertNotIn("schema", response_format)

    def test_each_role_sends_a_distinct_schema_name(self):
        names = [
            response_format["json_schema"]["name"]
            for response_format in ALL_RESPONSE_FORMATS.values()
        ]
        self.assertEqual(len(names), len(set(names)))

    def test_the_helper_nests_what_it_is_given(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        built = json_schema_response_format("probe", schema)
        self.assertEqual(built["json_schema"]["schema"], schema)
        self.assertEqual(built["json_schema"]["name"], "probe")

    def test_the_resolver_still_constrains_status_to_its_four_values(self):
        schema = TOOL_RESOLVER_RESPONSE_FORMAT["json_schema"]["schema"]
        self.assertEqual(
            schema["properties"]["status"]["enum"],
            ["found", "invalid_goal", "not_found", "unavailable"],
        )


if __name__ == "__main__":
    unittest.main()


class LocalModelSchemaCheckOperationTests(unittest.TestCase):
    """The operation exists, is read-only, and is discoverable by find_tool."""

    def _registry(self):
        from types import SimpleNamespace

        from modules.operations_registry import OperationsRegistry

        return OperationsRegistry(
            experiment_service=SimpleNamespace(resolve_experiment=lambda i: None)
        )

    def test_the_check_is_a_declared_read_only_capability(self):
        spec = {s.name: s for s in self._registry().catalog()}["local_model_schema_check"]
        self.assertFalse(spec.mutates_state)
        self.assertEqual(spec.arguments, [])

    def test_it_is_reachable_through_the_read_only_tool_surface(self):
        # find_tool only offers capabilities that declare themselves read-only,
        # so a wrong declaration would make it invisible to Nexus itself.
        from modules.nexus_tool_router import is_read_only

        spec = {s.name: s for s in self._registry().catalog()}["local_model_schema_check"]
        self.assertTrue(is_read_only(spec))

    def test_an_unreachable_server_fails_the_operation_without_claiming_more(self):
        import os

        previous = os.environ.get("LOCAL_MODEL_URL")
        os.environ["LOCAL_MODEL_URL"] = "http://127.0.0.1:9/v1"
        try:
            result = self._registry().execute("local_model_schema_check", {}, caller="test")
        finally:
            if previous is None:
                os.environ.pop("LOCAL_MODEL_URL", None)
            else:
                os.environ["LOCAL_MODEL_URL"] = previous

        self.assertFalse(result.success)
        self.assertIn("UNDETERMINED", "\n".join(result.output_lines))
