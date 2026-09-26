from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.mailbox import SUPPORTED_TYPES, _PROCESSORS
from modules.mailbox.diagnose import process_diagnose


class DiagnoseMailboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "agent.db"
        self.conn = sqlite3.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.conn.execute(
            """CREATE TABLE local_llm_jobs (
                   id TEXT PRIMARY KEY, status TEXT NOT NULL, prompt TEXT NOT NULL,
                   temperature REAL NOT NULL, max_tokens INTEGER NOT NULL,
                   response_format TEXT, session_id TEXT, created_at TEXT NOT NULL,
                   started_at TEXT, finished_at TEXT, result TEXT, error TEXT,
                   role TEXT NOT NULL DEFAULT 'controller', system TEXT,
                   error_kind TEXT, worker_id TEXT, lease_expires_at TEXT
               )"""
        )
        self.conn.execute(
            """INSERT INTO local_llm_jobs
                   (id,status,prompt,temperature,max_tokens,created_at,finished_at,result,role)
               VALUES ('job-1','succeeded','prompt text',0,550,'2026-01-01','2026-01-01',?,'controller')""",
            ('{"action":"find_tool","arguments":{"goal":"inspect repo"}}',),
        )
        self.conn.commit()

    def test_diagnose_is_registered(self) -> None:
        self.assertIn("diagnose", SUPPORTED_TYPES)
        self.assertIs(_PROCESSORS["diagnose"], process_diagnose)

    def test_generic_diagnose_message_returns_llm_job_result(self) -> None:
        msg = SimpleNamespace(
            body=json.dumps(
                {
                    "subject": "llm_job",
                    "reference": "job-1",
                    "include": ["prompt"],
                }
            )
        )
        body, idea_id = process_diagnose(msg, self.root, self.conn, 1)
        payload = json.loads(body.removeprefix("JSON diagnostic result:\n"))
        self.assertIsNone(idea_id)
        self.assertEqual("llm_job", payload["lifecycle"]["subject_type"])
        self.assertEqual(
            '{"action":"find_tool","arguments":{"goal":"inspect repo"}}',
            payload["lifecycle"]["related"]["result"],
        )
        self.assertEqual("prompt text", payload["details"]["prompt"])


if __name__ == "__main__":
    unittest.main()
