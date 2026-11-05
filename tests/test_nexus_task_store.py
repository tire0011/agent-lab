from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from modules.nexus_task_store import NexusTaskStore


class NexusTaskStoreEventIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = NexusTaskStore(db_path=str(Path(self.tmp.name) / "state.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_append_event_returns_a_stable_event_id(self):
        session = self.store.get_or_create(source_type="task", source_id="t-1")
        event_id = self.store.append_event(session.id, role="tool", kind="command", content="ok")
        self.assertIsInstance(event_id, int)
        self.assertGreater(event_id, 0)



    def test_recent_events_filters_raw_journal_events_newest_first(self):
        session = self.store.get_or_create(source_type="task", source_id="t-recent")
        other = self.store.get_or_create(source_type="task", source_id="t-other")
        self.store.append_event(session.id, role="tool", kind="find_tool", content='{"old":true}')
        self.store.append_event(other.id, role="tool", kind="find_tool", content='{"other":true}')
        newest_id = self.store.append_event(
            session.id,
            role="tool",
            kind="find_tool",
            content='{"new":true}',
            metadata={"reason": "grant"},
        )
        self.store.append_event(session.id, role="assistant", kind="answer", content="done")

        events = self.store.recent_events(
            session.id,
            role="tool",
            kind="find_tool",
        )

        self.assertEqual([event["content"] for event in events], ['{"new":true}', '{"old":true}'])
        self.assertEqual(events[0]["id"], newest_id)
        self.assertEqual(events[0]["metadata"], {"reason": "grant"})

    def test_history_hides_knowledge_collector_internal_events(self):
        session = self.store.get_or_create(source_type="task", source_id="t-knowledge")
        self.store.append_event(session.id, role="user", kind="input", content="solve issue")
        self.store.append_event(
            session.id,
            role="tool",
            kind="find_tool",
            content='{"internal":"source lookup"}',
            metadata={"knowledge_collector_internal": True},
        )
        self.store.append_event(
            session.id,
            role="tool",
            kind="knowledge_collector",
            content='{"knowledge_briefing":"compact facts"}',
        )

        history = self.store.history(session.id)
        contents = [event["content"] for event in history]

        self.assertIn("solve issue", contents)
        self.assertIn('{"knowledge_briefing":"compact facts"}', contents)
        self.assertNotIn('{"internal":"source lookup"}', contents)

    def test_history_with_max_event_id_excludes_later_events(self):
        session = self.store.get_or_create(source_type="task", source_id="t-3")
        anchor_id = self.store.append_event(session.id, role="user", kind="input", content="do work")
        self.store.append_event(session.id, role="tool", kind="command", content="later observation")

        bounded = self.store.history(session.id, max_event_id=anchor_id)
        contents = [event["content"] for event in bounded]
        self.assertIn("do work", contents)
        self.assertNotIn("later observation", contents)

        unbounded = self.store.history(session.id)
        contents_unbounded = [event["content"] for event in unbounded]
        self.assertIn("later observation", contents_unbounded)

    def test_user_inputs_are_full_ordered_and_skip_collector_internal_rows(self):
        session = self.store.get_or_create(source_type="task", source_id="t-inputs")
        original = "x" * 5000 + "\nUNIQUE-END-MARKER"
        self.store.append_event(session.id, role="user", kind="input", content=original)
        self.store.append_event(
            session.id,
            role="user",
            kind="input",
            content="internal lookup",
            metadata={"knowledge_collector_internal": True},
        )
        self.store.append_event(session.id, role="assistant", kind="answer", content="reply")
        self.store.append_event(session.id, role="user", kind="input", content="clarification")

        self.assertEqual(self.store.user_inputs(session.id), [original, "clarification"])
        self.assertEqual(len(self.store.history(session.id)[0]["content"]), 1800)


if __name__ == "__main__":
    unittest.main()
