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

    def test_get_event_returns_full_raw_content_by_id(self):
        session = self.store.get_or_create(source_type="task", source_id="t-2")
        big_content = "x" * 15000
        event_id = self.store.append_event(
            session.id, role="tool", kind="command", content=big_content, metadata={"reason": "test"}
        )
        event = self.store.get_event(event_id)
        self.assertIsNotNone(event)
        self.assertEqual(event["id"], event_id)
        self.assertEqual(event["content"], big_content)
        self.assertEqual(event["metadata"], {"reason": "test"})

    def test_get_event_returns_none_for_unknown_id(self):
        self.assertIsNone(self.store.get_event(999999))

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


if __name__ == "__main__":
    unittest.main()
