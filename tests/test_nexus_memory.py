from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from modules.nexus_memory import (
    ORCHESTRATION_EVENT_KINDS,
    NexusMemory,
    is_memory_eligible_event,
)
from modules.nexus_task_store import NexusTaskStore
from modules.run_store import init_db


class NexusMemoryTests(unittest.TestCase):
    def test_search_combines_knowledge_task_history_and_project_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = str(root / "state.db")
            conn = init_db(db_path)
            conn.execute(
                """INSERT INTO knowledge
                   (timestamp, run_id, category, key, value, confidence, source)
                   VALUES (?, NULL, ?, ?, ?, NULL, ?)""",
                (
                    "2026-08-23T00:00:00Z",
                    "architecture",
                    "delegation",
                    "Use local-first delegation to control external model cost.",
                    "test",
                ),
            )
            conn.commit()
            conn.close()

            store = NexusTaskStore(db_path=db_path)
            session = store.get_or_create(
                source_type="issue",
                source_id="46",
                title="Agent orchestration",
            )
            store.append_event(
                session.id,
                role="assistant",
                kind="answer",
                content="Memory should be available as a Nexus tool.",
            )
            (root / "DECISIONS.md").write_text(
                "# Decisions\n\nKeep the local model as the persistent controller.\n",
                encoding="utf-8",
            )

            memory = NexusMemory(
                db_path=db_path,
                repo_root=str(root),
                task_store=store,
            )
            try:
                delegation = memory.search("local-first delegation", limit=5)
                self.assertTrue(any(item["source"] == "knowledge" for item in delegation))

                tool_memory = memory.search("memory Nexus tool", limit=5)
                self.assertTrue(any(item["source"] == "task_history" for item in tool_memory))

                controller = memory.search("persistent controller", limit=5)
                self.assertTrue(
                    any(
                        item["source"] == "project_document"
                        and item.get("document") == "DECISIONS.md"
                        for item in controller
                    )
                )
            finally:
                memory.close()
                store.close()


    def test_explicit_issue_query_filters_other_issue_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = str(root / "state.db")
            store = NexusTaskStore(db_path=db_path)

            wrong = store.get_or_create(
                source_type="task",
                source_id="old-180",
                title="GitHub issue work",
            )
            store.append_event(
                wrong.id,
                role="assistant",
                kind="answer",
                content=(
                    "GitHub issue #180 GitHub issue GitHub issue: implement issue #180 "
                    "using the existing branch."
                ),
            )

            right = store.get_or_create(
                source_type="issue",
                source_id="238",
                title="memory_search indexes its own output",
            )
            store.append_event(
                right.id,
                role="assistant",
                kind="answer",
                content=(
                    "Issue #238: NexusMemory._search_task_events indexes memory_search "
                    "results recursively."
                ),
            )

            memory = NexusMemory(
                db_path=db_path,
                repo_root=str(root),
                task_store=store,
            )
            try:
                results = memory.search("GitHub issue #238", limit=10)
                self.assertTrue(results)
                self.assertTrue(
                    all(
                        "#180" not in item.get("content", "")
                        for item in results
                        if item.get("source") == "task_history"
                    )
                )
                self.assertTrue(
                    any(
                        item.get("source") == "task_history"
                        and "#238" in item.get("content", "")
                        for item in results
                    )
                )
            finally:
                memory.close()
                store.close()


    def test_memory_search_events_are_not_indexed_recursively(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = str(root / "state.db")
            store = NexusTaskStore(db_path=db_path)

            session = store.get_or_create(
                source_type="issue",
                source_id="238",
                title="memory_search indexes its own output",
            )
            store.append_event(
                session.id,
                role="tool",
                kind="memory_search",
                content=(
                    '{"tool":"memory_search","query":"GitHub issue #238",'
                    '"results":[{"content":"GitHub issue #196 with nested issue #180"}]}'
                ),
            )
            store.append_event(
                session.id,
                role="assistant",
                kind="answer",
                content=(
                    "Issue #238 root cause: NexusMemory indexes memory_search events, "
                    "which recursively nests earlier search results."
                ),
            )

            memory = NexusMemory(
                db_path=db_path,
                repo_root=str(root),
                task_store=store,
            )
            try:
                results = memory.search("GitHub issue #238", limit=10)
                self.assertTrue(results)
                self.assertFalse(
                    any(item.get("kind") == "memory_search" for item in results)
                )
                serialized = " ".join(item.get("content", "") for item in results)
                self.assertNotIn("issue #196", serialized)
                self.assertNotIn("issue #180", serialized)
                self.assertIn("Issue #238 root cause", serialized)
            finally:
                memory.close()
                store.close()

    def test_bookkeeping_events_do_not_crow_searchable_history_out_of_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = str(root / "state.db")
            store = NexusTaskStore(db_path=db_path)

            session = store.get_or_create(
                source_type="task",
                source_id="memory-filter",
                title="Memory filtering regression",
            )
            store.append_event(
                session.id,
                role="assistant",
                kind="answer",
                content="The durable searchable finding uses the term ultramarine.",
            )
            for index in range(501):
                store.append_event(
                    session.id,
                    role="tool",
                    kind="memory_search",
                    content=f'{{"tool":"memory_search","result":{index}}}',
                )

            memory = NexusMemory(
                db_path=db_path,
                repo_root=str(root),
                task_store=store,
            )
            try:
                results = memory.search("ultramarine", limit=10)
                self.assertTrue(
                    any(
                        item.get("kind") == "answer"
                        and "durable searchable finding" in item.get("content", "")
                        for item in results
                    )
                )
            finally:
                memory.close()
                store.close()

    def test_later_search_cannot_return_an_earlier_stored_search_result(self):
        """End to end (#248): search, store the result the way the orchestrator
        does (kind = tool name, content = serialized observation), search again.
        The second search must not return the first one, at any depth."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = str(root / "state.db")
            store = NexusTaskStore(db_path=db_path)
            session = store.get_or_create(
                source_type="issue", source_id="248", title="Event journal vs memory"
            )
            store.append_event(
                session.id,
                role="assistant",
                kind="answer",
                content="Issue #248 decision: the event journal is not curated memory.",
            )
            memory = NexusMemory(db_path=db_path, repo_root=str(root), task_store=store)
            try:
                query = "issue #248 event journal memory"
                first = memory.search(query, limit=10)
                self.assertEqual(len(first), 1)
                for _ in range(3):
                    observation = {
                        "tool": "memory_search",
                        "query": query,
                        "results": memory.search(query, limit=10),
                    }
                    store.append_event(
                        session.id,
                        role="tool",
                        kind=observation["tool"],
                        content=json.dumps(observation, separators=(",", ":")),
                    )
                later = memory.search(query, limit=10)
                self.assertEqual(later, first)
                for item in later:
                    self.assertNotEqual(item.get("kind"), "memory_search")
                    self.assertNotIn('"tool":"memory_search"', item.get("content", ""))
            finally:
                memory.close()
                store.close()

    def test_journal_keeps_bookkeeping_events_that_memory_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = str(root / "state.db")
            store = NexusTaskStore(db_path=db_path)
            session = store.get_or_create(
                source_type="task", source_id="journal", title="Journal completeness"
            )
            for kind in ORCHESTRATION_EVENT_KINDS:
                store.append_event(
                    session.id,
                    role="system",
                    kind=kind,
                    content=f"bookkeeping {kind} mentions the term cobaltblue",
                )
            store.append_event(
                session.id,
                role="assistant",
                kind="answer",
                content="Confirmed finding about cobaltblue.",
            )
            memory = NexusMemory(db_path=db_path, repo_root=str(root), task_store=store)
            try:
                results = memory.search("cobaltblue", limit=10)
                self.assertEqual([item["kind"] for item in results], ["answer"])
            finally:
                memory.close()

            conn = sqlite3.connect(db_path)
            try:
                stored = [
                    row[0]
                    for row in conn.execute(
                        "SELECT kind FROM nexus_task_events WHERE session_id=? ORDER BY id",
                        (session.id,),
                    )
                ]
            finally:
                conn.close()
                store.close()
            self.assertEqual(stored, [*ORCHESTRATION_EVENT_KINDS, "answer"])

    def test_memory_eligibility_policy_covers_required_bookkeeping_kinds(self):
        for kind in (
            "memory_search",
            "controller_decision",
            "find_tool",
            "find_tool_redirected",
            "redundant_tool_request",
            "terminal_decision_rejected",
            "knowledge_collection",
        ):
            self.assertFalse(is_memory_eligible_event(kind), kind)
        for kind in ("input", "answer", "change", "verify"):
            self.assertTrue(is_memory_eligible_event(kind), kind)


if __name__ == "__main__":
    unittest.main()
