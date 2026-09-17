"""The schema-enforcement probe's verdict must not be able to lie.

It exists to answer one question - does llama-server actually apply the
resolver's JSON schema - and a probe that says "enforced" for an unconstrained
server would send the next diagnosis after the wrong bug, which is exactly
what happened in #218.
"""

from __future__ import annotations

import json
import unittest

from scripts.check_llama_json_schema import PROBE_SCHEMA, PROMPT, classify


class LlamaJsonSchemaProbeTests(unittest.TestCase):
    def test_only_the_constrained_answer_counts_as_enforced(self):
        verdict, _ = classify(json.dumps({"status": "found"}))
        self.assertEqual(verdict, "ENFORCED")

    def test_the_answer_the_prompt_asked_for_proves_it_is_not_enforced(self):
        # The whole design: a constrained decoder cannot emit this.
        verdict, detail = classify(json.dumps({"status": "banana"}))
        self.assertEqual(verdict, "NOT ENFORCED")
        self.assertIn("banana", detail)

    def test_unparsable_and_non_object_answers_are_not_enforced(self):
        for content in ("banana", "[]", '{"status":', '"found"'):
            with self.subTest(content=content):
                self.assertEqual(classify(content)[0], "NOT ENFORCED")

    def test_the_probe_asks_for_something_the_schema_forbids(self):
        # If these ever drift apart the probe proves nothing: the model could
        # satisfy both the prompt and the schema.
        allowed = PROBE_SCHEMA["properties"]["status"]["enum"]
        self.assertEqual(allowed, ["found"])
        self.assertIn("banana", PROMPT)
        self.assertNotIn("banana", allowed)


if __name__ == "__main__":
    unittest.main()
