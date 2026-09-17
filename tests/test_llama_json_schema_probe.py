"""The schema-enforcement probe's verdict must not be able to lie.

It exists to answer one question - does llama-server actually apply the
resolver's JSON schema - and a probe that says "enforced" for an unconstrained
server would send the next diagnosis after the wrong bug, which is exactly
what happened in #218.
"""

from __future__ import annotations

import json
import unittest

from modules.llm_schema_probe import (
    ENFORCED,
    NOT_ENFORCED,
    PROBE_SCHEMA,
    PROMPT,
    classify,
    verdict,
)


class LlamaJsonSchemaProbeTests(unittest.TestCase):
    def test_only_the_constrained_answer_counts_as_enforced(self):
        outcome, _ = classify(json.dumps({"status": "found"}))
        self.assertEqual(outcome, ENFORCED)

    def test_the_answer_the_prompt_asked_for_proves_it_is_not_enforced(self):
        # The whole design: a constrained decoder cannot emit this.
        outcome, detail = classify(json.dumps({"status": "banana"}))
        self.assertEqual(outcome, NOT_ENFORCED)
        self.assertIn("banana", detail)

    def test_unparsable_and_non_object_answers_are_not_enforced(self):
        for content in ("banana", "[]", '{"status":', '"found"'):
            with self.subTest(content=content):
                self.assertEqual(classify(content)[0], NOT_ENFORCED)

    def test_an_unreachable_server_is_undetermined_not_unenforced(self):
        # "Could not reach it" and "it ignores the schema" are different
        # claims, and reporting the second for the first is the misdiagnosis
        # this probe exists because of.
        healthy, lines = verdict([("nexus", "ERROR", "connection refused"),
                                  ("control", "ERROR", "connection refused")])
        self.assertFalse(healthy)
        self.assertIn("UNDETERMINED", lines[0])
        self.assertNotIn("NOT being applied", lines[0])

    def test_a_control_that_complies_makes_the_probe_inconclusive(self):
        # If the model returns the constrained value with no schema at all,
        # a pass would be coincidence rather than evidence.
        healthy, lines = verdict([("nexus", ENFORCED, "{}"), ("control", ENFORCED, "{}")])
        self.assertFalse(healthy)
        self.assertIn("INCONCLUSIVE", lines[0])

    def test_a_working_shape_is_named_when_nexus_own_shape_fails(self):
        healthy, lines = verdict([
            ("nexus", NOT_ENFORCED, "banana"),
            ("llama_cpp_native", ENFORCED, "found"),
            ("control", NOT_ENFORCED, "banana"),
        ])
        self.assertFalse(healthy)
        self.assertTrue(any("llama_cpp_native" in line for line in lines))

    def test_the_probe_asks_for_something_the_schema_forbids(self):
        # If these ever drift apart the probe proves nothing: the model could
        # satisfy both the prompt and the schema.
        allowed = PROBE_SCHEMA["properties"]["status"]["enum"]
        self.assertEqual(allowed, ["found"])
        self.assertIn("banana", PROMPT)
        self.assertNotIn("banana", allowed)


if __name__ == "__main__":
    unittest.main()
