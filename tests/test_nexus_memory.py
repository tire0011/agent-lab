from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from modules.nexus_memory import NexusMemory
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


if __name__ == "__main__":
    unittest.main()
