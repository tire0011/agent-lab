from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules.mailbox_task_service import MailboxTaskService, _STATUS_NEEDS_REVIEW
from modules.run_store import init_db
from modules.task_intent import ACTION_CLARIFICATION, ACTION_MISSING_CAPABILITY


def _service(tmp: Path, experiment_service: MagicMock) -> MailboxTaskService:
    db_path = str(tmp / "test.db")
    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO mailbox_messages (file_path, source_commit_sha, msg_type, created_by, status, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("/agent/inbox/test.md", "abc", "task", "tester", "done", "2026-01-01T00:00:00Z"),
    )
    msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    svc = MailboxTaskService(
        db_path=db_path,
        experiment_service=experiment_service,
        outbox_dir=str(tmp / "outbox"),
        repo_path=str(tmp),
    )
    approval = svc.create_approval(
        msg_id, "ambiguous task", "tester", "/agent/inbox/test.md", "2026-01-01T00:00:00Z"
    )
    svc.approve(approval.id)
    svc._test_approval_id = approval.id
    return svc


class MailboxWorkflowRoutingTests(unittest.TestCase):
    def test_clarification_does_not_create_experiment(self):
        tmp = Path(tempfile.mkdtemp())
        es = MagicMock()
        svc = _service(tmp, es)
        decision = SimpleNamespace(
            action=ACTION_CLARIFICATION,
            experiment_id=None,
            reason="material ambiguity",
            message="Which repository should I change?",
            source="local_llm",
        )
        with patch("modules.mailbox_task_service.classify_task_intent", return_value=decision):
            svc.submit_approved()

        es.create_experiment.assert_not_called()
        result = svc.get(svc._test_approval_id)
        self.assertEqual(result.status, _STATUS_NEEDS_REVIEW)
        self.assertIsNone(result.experiment_id)
        self.assertIn("Which repository", result.result_summary)
        self.assertTrue(list((tmp / "outbox").glob("*.md")))

    def test_missing_capability_does_not_create_experiment(self):
        tmp = Path(tempfile.mkdtemp())
        es = MagicMock()
        svc = _service(tmp, es)
        decision = SimpleNamespace(
            action=ACTION_MISSING_CAPABILITY,
            experiment_id=None,
            reason="tool unavailable",
            message="I need a calendar tool to complete this request.",
            source="local_llm",
        )
        with patch("modules.mailbox_task_service.classify_task_intent", return_value=decision):
            svc.submit_approved()

        es.create_experiment.assert_not_called()
        result = svc.get(svc._test_approval_id)
        self.assertEqual(result.status, _STATUS_NEEDS_REVIEW)
        self.assertIn("calendar tool", result.result_summary)


if __name__ == "__main__":
    unittest.main()
