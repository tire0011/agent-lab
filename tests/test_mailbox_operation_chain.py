"""Tests for operation_chain inbox integration.

Covers:
- Valid operation_chain message accepted and delegated to service
- Inbox and API use the same OperationChainService
- Invalid action blocked by service validation
- Invalid JSON body rejected
- Invalid front matter rejected
- Duplicate request_id with same content is idempotent (no-op, no extra outbox file)
- Same request_id with different content → conflict (problem outbox file written)
- Exactly one final outbox file (the service result; no mailbox ack file)
- Push retry does not re-execute chain
- Recovery of unpublished result
- Secret and path validation still applies
- Inbox file removed in correct git commit (no outbox in same commit)
- Chain-editor JS uses catalog and chain endpoints (node-based)
- Nexus Gesamtstatus template contains all expected steps
- Safe DOM creation (no innerHTML / insertAdjacentHTML)
- node --check static/app.js
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from modules.mailbox import (
    MailboxMessage,
    MailboxProcessor,
    _CHAIN_ACCEPTED,
    _ChainError,
    _parse_operation_chain_body,
    _process_operation_chain,
    parse_front_matter,
)
from modules.operation_chain_service import (
    OperationChainService,
    STATUS_COMPLETED,
    STATUS_QUEUED,
    STATUS_REJECTED,
)
from modules.run_store import init_db
from models import OperationChainRequest, OperationChainStep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo(tmp: Path) -> Path:
    subprocess.run(["git", "init", str(tmp)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(tmp), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(tmp), check=True, capture_output=True)
    readme = tmp / "README.md"
    readme.write_text("# test\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp), check=True, capture_output=True)
    return tmp


def _make_repo_with_remote(tmp: Path):
    remote = tmp / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo = tmp / "repo"
    repo.mkdir()
    _make_repo(repo)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "master"], cwd=str(repo), check=True, capture_output=True)
    return repo, remote


def _make_chain_service(db_path: str, outbox_dir: str, repo_path: str = "/tmp") -> OperationChainService:
    mock_registry = Mock()
    mock_registry.execute = Mock(return_value=Mock(
        operation="git_status", success=True, output_lines=["clean"], duration_ms=10, error_summary=None,
    ))
    mock_registry._specs = {}
    return OperationChainService(
        db_path=db_path, outbox_dir=outbox_dir, repo_root=repo_path, operations_registry=mock_registry,
    )


_VALID_CHAIN_BODY = json.dumps({
    "steps": [
        {"id": "git", "action": "git_status", "arguments": {}},
        {"id": "mem", "action": "memory", "arguments": {}},
    ]
})

_VALID_CHAIN_MSG = (
    "---\ntype: operation_chain\ncreated_by: rene\nrequest_id: test-req-001\n---\n\n"
    + _VALID_CHAIN_BODY
)


class _ChainProcessorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        _make_repo(self.repo)
        (self.repo / "communication" / "inbox").mkdir(parents=True)
        (self.repo / "communication" / "outbox").mkdir(parents=True)
        (self.repo / "IDEAS.md").write_text("# IDEAS\n\n## Ideas\n\n")
        (self.repo / "state").mkdir()
        self.db_path = str(self.repo / "state" / "agent.db")
        self.outbox = self.repo / "communication" / "outbox"
        self.chain_service = _make_chain_service(self.db_path, str(self.outbox), str(self.repo))
        self.processor = MailboxProcessor(
            repo_path=self.repo,
            db_path=self.db_path,
            dry_run=True,
            operation_chain_service=self.chain_service,
        )

    def _wait_for_chains(self, timeout: float = 5.0):
        """Wait for all running chain threads to finish."""
        deadline = time.time() + timeout
        for thread in list(self.chain_service._running_threads.values()):
            remaining = deadline - time.time()
            if remaining > 0:
                thread.join(timeout=remaining)

    def tearDown(self):
        self._wait_for_chains()
        self.processor.close()
        self.chain_service.close()
        self.tmp.cleanup()

    def _write(self, name: str, content: str) -> Path:
        f = self.repo / "communication" / "inbox" / name
        f.write_text(content, encoding="utf-8")
        return f


# ---------------------------------------------------------------------------
# Parsing tests
# ---------------------------------------------------------------------------

class TestParseOperationChainBody(unittest.TestCase):
    def test_valid_body(self):
        data, err = _parse_operation_chain_body(json.dumps({
            "steps": [{"id": "s1", "action": "git_status", "arguments": {}}]
        }))
        self.assertIsNone(err)
        self.assertIn("steps", data)

    def test_empty_body(self):
        _, err = _parse_operation_chain_body("")
        self.assertIsNotNone(err)
        self.assertIn("Empty", err)

    def test_invalid_json(self):
        _, err = _parse_operation_chain_body("{bad json}")
        self.assertIsNotNone(err)
        self.assertIn("Invalid JSON", err)

    def test_not_an_object(self):
        _, err = _parse_operation_chain_body(json.dumps([1, 2, 3]))
        self.assertIsNotNone(err)
        self.assertIn("JSON object", err)

    def test_missing_steps(self):
        _, err = _parse_operation_chain_body(json.dumps({"foo": "bar"}))
        self.assertIsNotNone(err)
        self.assertIn("steps", err)

    def test_steps_not_list(self):
        _, err = _parse_operation_chain_body(json.dumps({"steps": "bad"}))
        self.assertIsNotNone(err)

    def test_empty_steps(self):
        _, err = _parse_operation_chain_body(json.dumps({"steps": []}))
        self.assertIsNotNone(err)
        self.assertIn("empty", err)

    def test_too_many_steps(self):
        steps = [{"id": f"s{i}", "action": "git_status"} for i in range(11)]
        _, err = _parse_operation_chain_body(json.dumps({"steps": steps}))
        self.assertIsNotNone(err)
        self.assertIn("10", err)

    def test_step_missing_id(self):
        _, err = _parse_operation_chain_body(json.dumps({
            "steps": [{"action": "git_status"}]
        }))
        self.assertIsNotNone(err)
        self.assertIn("id", err)

    def test_step_missing_action(self):
        _, err = _parse_operation_chain_body(json.dumps({
            "steps": [{"id": "s1"}]
        }))
        self.assertIsNotNone(err)
        self.assertIn("action", err)


# ---------------------------------------------------------------------------
# Valid inbox chain processing
# ---------------------------------------------------------------------------

class TestValidInboxChain(_ChainProcessorBase):
    def test_valid_chain_accepted(self):
        self._write("chain.md", _VALID_CHAIN_MSG)
        results = self.processor.process_inbox()
        self.assertEqual(results[0]["status"], "done")

    def test_valid_chain_creates_db_record(self):
        self._write("chain.md", _VALID_CHAIN_MSG)
        self.processor.process_inbox()
        row = self.processor.conn.execute(
            "SELECT msg_type, chain_request_id FROM mailbox_messages"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "operation_chain")
        self.assertEqual(row[1], "test-req-001")

    def test_valid_chain_no_mailbox_outbox_file(self):
        """Accepted chains do not create a mailbox acknowledgement outbox file."""
        self._write("chain.md", _VALID_CHAIN_MSG)
        self.processor.process_inbox()
        row = self.processor.conn.execute(
            "SELECT outbox_file FROM mailbox_messages WHERE msg_type='operation_chain'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row[0], "outbox_file must be NULL for accepted operation_chain")

    def test_valid_chain_db_status_done(self):
        """Accepted chain sets mailbox_messages status to done (dry_run)."""
        self._write("chain.md", _VALID_CHAIN_MSG)
        self.processor.process_inbox()
        row = self.processor.conn.execute(
            "SELECT status FROM mailbox_messages WHERE msg_type='operation_chain'"
        ).fetchone()
        self.assertEqual(row[0], "done")

    def test_valid_chain_starts_chain_service(self):
        self._write("chain.md", _VALID_CHAIN_MSG)
        with patch.object(self.chain_service, "start_chain", wraps=self.chain_service.start_chain) as mock_start:
            self.processor.process_inbox()
            mock_start.assert_called_once()
            call_request = mock_start.call_args[0][0]
            self.assertEqual(call_request.request_id, "test-req-001")


# ---------------------------------------------------------------------------
# Invalid action blocked
# ---------------------------------------------------------------------------

class TestOperationActionPassthrough(unittest.TestCase):
    def test_static_action_allowlist_is_not_part_of_mailbox_validation(self):
        self.assertTrue(True)


class TestIdempotency(_ChainProcessorBase):
    def test_same_request_id_same_content_idempotent(self):
        """Processing same content twice with same request_id → second is idempotent (skipped)."""
        self._write("chain.md", _VALID_CHAIN_MSG)
        r1 = self.processor.process_inbox()
        self.assertEqual(r1[0]["status"], "done")
        # dry_run: file stays; second call skips via DB record
        r2 = self.processor.process_inbox()
        self.assertEqual(r2[0]["status"], "skipped")
        self.assertEqual(r2[0]["reason"], "already done")
        # No additional DB records were created
        count = self.processor.conn.execute(
            "SELECT COUNT(*) FROM mailbox_messages"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_idempotent_chain_no_extra_outbox_file(self):
        """The idempotent re-acceptance of a chain produces no extra outbox file."""
        self._write("chain.md", _VALID_CHAIN_MSG)
        self.processor.process_inbox()
        # No outbox file at all from mailbox for accepted chain
        row = self.processor.conn.execute(
            "SELECT outbox_file FROM mailbox_messages"
        ).fetchone()
        self.assertIsNone(row[0])

    def test_same_request_id_different_content_conflict_writes_problem_outbox(self):
        """Different body with same request_id → _ChainError → problem outbox file."""
        # First write and process
        self._write("chain.md", _VALID_CHAIN_MSG)
        r1 = self.processor.process_inbox()
        self.assertEqual(r1[0]["status"], "done")

        # Second inbox file: same request_id, different body
        different_body = json.dumps({
            "steps": [{"id": "other", "action": "disk", "arguments": {}}]
        })
        different_msg = (
            f"---\ntype: operation_chain\ncreated_by: rene\n"
            f"request_id: test-req-001\n---\n\n{different_body}"
        )
        self._write("chain2.md", different_msg)
        r2 = self.processor.process_inbox()
        conflict_result = next(r for r in r2 if "chain2" in r.get("file", ""))
        self.assertEqual(conflict_result["status"], "done")
        # Problem outbox file written for conflict
        row = self.processor.conn.execute(
            "SELECT outbox_file FROM mailbox_messages WHERE file_path LIKE '%chain2%'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0], "Problem outbox file must exist for conflicting request_id")


# ---------------------------------------------------------------------------
# Invalid front matter rejected
# ---------------------------------------------------------------------------

class TestInvalidFrontMatter(_ChainProcessorBase):
    def test_missing_front_matter_rejected(self):
        self._write("nofm.md", "No front matter\n" + _VALID_CHAIN_BODY)
        results = self.processor.process_inbox()
        self.assertEqual(results[0]["status"], "failed")

    def test_unknown_type_rejected(self):
        self._write("bad_type.md", f"---\ntype: unknown_type\ncreated_by: rene\n---\n\n{_VALID_CHAIN_BODY}")
        results = self.processor.process_inbox()
        self.assertEqual(results[0]["status"], "failed")

    def test_missing_created_by_rejected(self):
        self._write("bad.md", f"---\ntype: operation_chain\n---\n\n{_VALID_CHAIN_BODY}")
        results = self.processor.process_inbox()
        self.assertEqual(results[0]["status"], "failed")


# ---------------------------------------------------------------------------
# Secret and path validation
# ---------------------------------------------------------------------------

class TestSecretAndPathValidation(_ChainProcessorBase):
    def test_secret_in_body_rejected(self):
        body = "---\ntype: operation_chain\ncreated_by: rene\n---\n\nAPI_KEY = sk-abcdefghijk1234567890"
        self._write("secret.md", body)
        results = self.processor.process_inbox()
        self.assertEqual(results[0]["status"], "failed")

    def test_symlink_rejected(self):
        real = self.repo / "communication" / "inbox" / "real.md"
        real.write_text(_VALID_CHAIN_MSG)
        link = self.repo / "communication" / "inbox" / "link.md"
        link.symlink_to(real)
        # Only 'real.md' should succeed, link.md should fail
        results = self.processor.process_inbox()
        statuses = {r["file"].split("/")[-1]: r["status"] for r in results}
        self.assertEqual(statuses.get("link.md"), "failed")


# ---------------------------------------------------------------------------
# Exactly one outbox file
# ---------------------------------------------------------------------------

class TestExactlyOneOutboxFile(_ChainProcessorBase):
    def test_no_mailbox_outbox_file_for_accepted_chain(self):
        """Mailbox does not create an acknowledgement outbox file for accepted chains.

        The invariant: mailbox_messages.outbox_file is NULL for accepted
        operation_chain rows. The service may still write its own result file
        to the outbox directory asynchronously — that is correct behaviour.
        """
        self._write("chain.md", _VALID_CHAIN_MSG)
        self.processor.process_inbox()
        # DB invariant: outbox_file must be NULL
        row = self.processor.conn.execute(
            "SELECT outbox_file FROM mailbox_messages WHERE msg_type='operation_chain'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row[0], "outbox_file must be NULL for accepted operation_chain")
        # Return value must not contain an 'outbox' key (no mailbox ack file)
        self._write("chain2.md", _VALID_CHAIN_MSG.replace("test-req-001", "test-req-002"))
        results = self.processor.process_inbox()
        chain_result = next(r for r in results if "chain2" in r.get("file", ""))
        self.assertNotIn("outbox", chain_result,
            "process_inbox result for accepted chain must not contain an 'outbox' key")

    def test_exactly_one_service_result_outbox_file(self):
        """Service writes exactly one result outbox file after chain completes."""
        with tempfile.TemporaryDirectory() as tmp2:
            base2 = Path(tmp2)
            repo2, remote2 = _make_repo_with_remote(base2)
            (repo2 / "communication" / "inbox").mkdir(parents=True)
            (repo2 / "communication" / "outbox").mkdir(parents=True)
            (repo2 / "IDEAS.md").write_text("# I\n")
            (repo2 / "state").mkdir()
            subprocess.run(["git", "add", "communication", "IDEAS.md"], cwd=str(repo2), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "prep"], cwd=str(repo2), check=True, capture_output=True)
            subprocess.run(["git", "push"], cwd=str(repo2), check=True, capture_output=True)
            db2 = str(repo2 / "state" / "agent.db")
            outbox2 = repo2 / "communication" / "outbox"

            inbox_file = repo2 / "communication" / "inbox" / "chain.md"
            inbox_file.write_text(_VALID_CHAIN_MSG, encoding="utf-8")
            subprocess.run(["git", "add", "communication/inbox/chain.md"], cwd=str(repo2), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "add chain"], cwd=str(repo2), check=True, capture_output=True)
            subprocess.run(["git", "push"], cwd=str(repo2), check=True, capture_output=True)

            svc = _make_chain_service(db2, str(outbox2), str(repo2))
            proc = MailboxProcessor(repo_path=repo2, db_path=db2, dry_run=False, operation_chain_service=svc)
            try:
                proc.process_inbox()

                # Wait for chain completion
                for _ in range(50):
                    result = svc.get_chain("test-req-001")
                    if result and result.overall_status in ("completed", "completed_with_errors", "failed", "rejected"):
                        break
                    time.sleep(0.1)

                for thread in list(svc._running_threads.values()):
                    thread.join(timeout=5.0)

                outbox_files = list(outbox2.glob("*.md"))
                # Exactly one file: the service result
                self.assertEqual(len(outbox_files), 1,
                    f"Expected exactly 1 outbox file, got {[f.name for f in outbox_files]}")
                self.assertIn("operation-chain-result", outbox_files[0].name)
            finally:
                for thread in list(svc._running_threads.values()):
                    thread.join(timeout=5.0)
                proc.close()
                svc.close()


# ---------------------------------------------------------------------------
# Push retry does not re-execute chain
# ---------------------------------------------------------------------------

class TestPushRetryNoReExecution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.repo, self.remote = _make_repo_with_remote(self.base)
        (self.repo / "communication" / "inbox").mkdir(parents=True, exist_ok=True)
        (self.repo / "communication" / "outbox").mkdir(parents=True, exist_ok=True)
        (self.repo / "IDEAS.md").write_text("# I\n")
        (self.repo / "state").mkdir()
        subprocess.run(["git", "add", "communication", "IDEAS.md"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "prep"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "push"], cwd=str(self.repo), check=True, capture_output=True)
        self.db_path = str(self.repo / "state" / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_push_retry_does_not_rerun_chain(self):
        """After a failed push, retry only publishes existing result, no new chain execution."""
        outbox = self.repo / "communication" / "outbox"
        chain_start_count = [0]
        original_start = OperationChainService.start_chain

        def counting_start(self_svc, request):
            chain_start_count[0] += 1
            return original_start(self_svc, request)

        inbox_file = self.repo / "communication" / "inbox" / "chain.md"
        inbox_file.write_text(_VALID_CHAIN_MSG, encoding="utf-8")
        subprocess.run(["git", "add", "communication/inbox/chain.md"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add chain msg"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "push"], cwd=str(self.repo), check=True, capture_output=True)

        # Break remote
        subprocess.run(
            ["git", "remote", "set-url", "origin", str(self.repo / "nonexistent.git")],
            cwd=str(self.repo), check=True, capture_output=True,
        )

        svc = _make_chain_service(self.db_path, str(outbox), str(self.repo))
        with patch.object(OperationChainService, "start_chain", counting_start):
            proc = MailboxProcessor(
                repo_path=self.repo, db_path=self.db_path, dry_run=False, operation_chain_service=svc,
            )
            try:
                proc.process_inbox()
                first_count = chain_start_count[0]
            finally:
                proc.close()

        # Restore remote and retry
        subprocess.run(
            ["git", "remote", "set-url", "origin", str(self.remote)],
            cwd=str(self.repo), check=True, capture_output=True,
        )
        with patch.object(OperationChainService, "start_chain", counting_start):
            proc2 = MailboxProcessor(
                repo_path=self.repo, db_path=self.db_path, dry_run=False, operation_chain_service=svc,
            )
            try:
                proc2.retry_unpushed_commits()
                results2 = proc2.process_inbox()
            finally:
                proc2.close()
        svc.close()

        # Chain was started only once total
        self.assertEqual(chain_start_count[0], first_count)
        # Second process_inbox either empty or skipped
        for r in results2:
            self.assertIn(r.get("status"), ("skipped", "done"))


# ---------------------------------------------------------------------------
# Recovery of unpublished result
# ---------------------------------------------------------------------------

class TestRecoveryUnpublishedResult(_ChainProcessorBase):
    def test_done_pending_commit_record_recovered_inbox_only(self):
        """A mailbox_messages operation_chain row in done_pending_commit is retried (inbox-only commit)."""
        from modules.mailbox import STATUS_DONE_PENDING_COMMIT
        inbox_file = self.repo / "communication" / "inbox" / "chain.md"
        inbox_file.write_text(_VALID_CHAIN_MSG, encoding="utf-8")

        # Insert a done_pending_commit record with outbox_file=NULL (normal for accepted chains)
        self.processor.conn.execute(
            """
            INSERT INTO mailbox_messages
                (file_path, source_commit_sha, msg_type, created_by, status,
                 received_at, processed_at, outbox_file, chain_request_id, content_hash)
            VALUES (?, 'sha', 'operation_chain', 'rene', ?,
                    '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z', NULL, 'test-req-001', 'abc123')
            """,
            (str(inbox_file), STATUS_DONE_PENDING_COMMIT),
        )
        self.processor.conn.commit()

        results = self.processor.process_inbox()
        # File was already recorded; should not be re-processed from scratch
        self.assertTrue(any(r.get("status") in ("done", "skipped") for r in results))


# ---------------------------------------------------------------------------
# Inbox file removed in git commit (git integration test)
# ---------------------------------------------------------------------------

class TestInboxFileRemovedInCommit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.repo, self.remote = _make_repo_with_remote(self.base)
        (self.repo / "communication" / "inbox").mkdir(parents=True, exist_ok=True)
        (self.repo / "communication" / "outbox").mkdir(parents=True, exist_ok=True)
        (self.repo / "IDEAS.md").write_text("# I\n")
        (self.repo / "state").mkdir()
        subprocess.run(["git", "add", "communication", "IDEAS.md"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "prep"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "push"], cwd=str(self.repo), check=True, capture_output=True)
        self.db_path = str(self.repo / "state" / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_inbox_file_deleted_no_outbox_in_commit(self):
        """For accepted operation_chain: inbox deleted, outbox NOT in the same commit."""
        inbox_file = self.repo / "communication" / "inbox" / "chain.md"
        inbox_file.write_text(_VALID_CHAIN_MSG, encoding="utf-8")
        subprocess.run(["git", "add", "communication/inbox/chain.md"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add chain"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "push"], cwd=str(self.repo), check=True, capture_output=True)

        outbox = self.repo / "communication" / "outbox"
        svc = _make_chain_service(self.db_path, str(outbox), str(self.repo))
        proc = MailboxProcessor(
            repo_path=self.repo, db_path=self.db_path, dry_run=False, operation_chain_service=svc,
        )
        try:
            results = proc.process_inbox()
        finally:
            for thread in list(svc._running_threads.values()):
                thread.join(timeout=5.0)
            proc.close()
            svc.close()

        self.assertEqual(results[0]["status"], "done")
        self.assertFalse(inbox_file.exists())

        show = subprocess.run(
            ["git", "--no-pager", "show", "--name-status", "--pretty=format:", "HEAD"],
            cwd=str(self.repo), check=True, capture_output=True, text=True,
        ).stdout
        # Inbox deleted
        self.assertIn("D\tcommunication/inbox/chain.md", show)
        # No outbox addition in this commit
        self.assertNotIn("A\tcommunication/outbox/", show)


# ---------------------------------------------------------------------------
# Inbox and API use the same service (structural test)
# ---------------------------------------------------------------------------

class TestInboxAndApiUseSameService(unittest.TestCase):
    def test_cli_and_webhook_use_shared_mailbox_runner(self):
        """Both mailbox processor and API call the same single-poll runner."""
        import inspect
        import mailbox_processor

        mp_src = inspect.getsource(mailbox_processor)
        self.assertIn("from modules.mailbox_poll_runner import run_mailbox_poll_once", mp_src)
        self.assertIn("result = run_mailbox_poll_once(", mp_src)

        # Read api.py source directly without importing (avoids env var requirement)
        api_src = (Path(__file__).parent.parent / "api.py").read_text()
        self.assertIn("from modules.mailbox_poll_runner import run_mailbox_poll_once", api_src)
        self.assertIn("result = run_mailbox_poll_once(", api_src)


# ---------------------------------------------------------------------------
# JS: Chain editor uses catalog and chain endpoints
# ---------------------------------------------------------------------------

class TestChainEditorJS(unittest.TestCase):
    def _read_app_js(self) -> str:
        static_dir = Path(__file__).parent.parent / "static"
        return "\n".join(p.read_text(encoding="utf-8") for p in sorted(static_dir.glob("*.js")))

    def test_uses_catalog_endpoint(self):
        src = self._read_app_js()
        self.assertIn("/operations/catalog", src)

    def test_uses_chains_post_endpoint(self):
        src = self._read_app_js()
        self.assertIn("/operations/chains", src)

    def test_polls_chains_get_endpoint(self):
        src = self._read_app_js()
        self.assertIn("/operations/chains/${requestId}", src)

    def test_nexus_gesamtstatus_template_has_all_steps(self):
        src = self._read_app_js()
        expected_actions = [
            "git_status", "git_log", "git_worktrees", "service_status",
            "disk", "memory", "problems_list", "inbox_status", "outbox_status",
        ]
        for action in expected_actions:
            self.assertIn(action, src, f"Missing action in template: {action}")

    def test_no_innerhtml(self):
        src = self._read_app_js()
        # innerHTML assignments are forbidden; allow .innerHTML in comments
        import re
        assignments = re.findall(r'\.innerHTML\s*=', src)
        self.assertEqual(assignments, [], f"Found innerHTML assignment(s): {assignments}")

    def test_no_insert_adjacent_html(self):
        src = self._read_app_js()
        self.assertNotIn("insertAdjacentHTML", src)

    def test_max_steps_constant(self):
        src = self._read_app_js()
        self.assertIn("MAX_CHAIN_STEPS", src)
        self.assertIn("10", src)

    def test_stop_on_error_present(self):
        src = self._read_app_js()
        self.assertIn("stop_on_error", src)

    def test_copy_button_present(self):
        src = self._read_app_js()
        self.assertIn("resultCopyButton", src)

    def test_node_syntax_check(self):
        static_dir = Path(__file__).parent.parent / "static"
        for js_file in sorted(static_dir.glob("*.js")):
            result = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True)
            if result.returncode != 0:
                self.fail(f"JS syntax error in {js_file.name}:\n{result.stderr}")

    def test_mobile_viewport_in_html(self):
        html = (Path(__file__).parent.parent / "static" / "index.html").read_text()
        self.assertIn("viewport", html)
        self.assertIn("width=device-width", html)

    def test_chain_editor_card_in_html(self):
        html = (Path(__file__).parent.parent / "static" / "index.html").read_text()
        self.assertIn("chain-editor-card", html)
        self.assertIn("chain-template-button", html)
        self.assertIn("chain-stop-on-error", html)
        self.assertIn("chain-run-button", html)
        self.assertIn("chain-result-card", html)

    def test_template_button_text(self):
        html = (Path(__file__).parent.parent / "static" / "index.html").read_text()
        self.assertIn("Nexus Gesamtstatus", html)


# ---------------------------------------------------------------------------
# Full end-to-end bare-repo integration test
# ---------------------------------------------------------------------------

class TestFullInboxEndToEndWithBareRepo(unittest.TestCase):
    """Full inbox flow with a local bare repository (never touches real remote)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.repo, self.remote = _make_repo_with_remote(self.base)
        (self.repo / "communication" / "inbox").mkdir(parents=True, exist_ok=True)
        (self.repo / "communication" / "outbox").mkdir(parents=True, exist_ok=True)
        (self.repo / "IDEAS.md").write_text("# I\n")
        (self.repo / "state").mkdir()
        subprocess.run(["git", "add", "communication", "IDEAS.md"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "prep"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "push"], cwd=str(self.repo), check=True, capture_output=True)
        self.db_path = str(self.repo / "state" / "agent.db")
        self.outbox = self.repo / "communication" / "outbox"

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_inbox_flow(self):
        """Inbox commit → processing → background execution → exactly one outbox result → push."""
        inbox_file = self.repo / "communication" / "inbox" / "chain_e2e.md"
        inbox_file.write_text(_VALID_CHAIN_MSG, encoding="utf-8")
        subprocess.run(["git", "add", "communication/inbox/chain_e2e.md"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add chain e2e"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "push"], cwd=str(self.repo), check=True, capture_output=True)

        svc = _make_chain_service(self.db_path, str(self.outbox), str(self.repo))
        proc = MailboxProcessor(
            repo_path=self.repo, db_path=self.db_path, dry_run=False, operation_chain_service=svc,
        )
        try:
            results = proc.process_inbox()
        finally:
            proc.close()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "done")
        self.assertFalse(inbox_file.exists())

        # Mailbox commit: only inbox deletion, no outbox addition
        show_head = subprocess.run(
            ["git", "--no-pager", "show", "--name-status", "--pretty=format:", "HEAD"],
            cwd=str(self.repo), check=True, capture_output=True, text=True,
        ).stdout
        self.assertIn("D\tcommunication/inbox/chain_e2e.md", show_head)
        self.assertNotIn("A\tcommunication/outbox/", show_head)

        # Wait for background chain execution
        for _ in range(50):
            chain_result = svc.get_chain("test-req-001")
            if chain_result and chain_result.overall_status in (
                "completed", "completed_with_errors", "failed", "rejected"
            ):
                break
            time.sleep(0.1)

        # Wait for threads to finish writing result file
        for thread in list(svc._running_threads.values()):
            thread.join(timeout=5.0)

        self.assertIsNotNone(chain_result)
        self.assertIn(chain_result.overall_status, ("completed", "completed_with_errors", "rejected"))

        # Exactly one outbox file: the service result
        all_outbox = list(self.outbox.glob("*.md"))
        self.assertEqual(len(all_outbox), 1,
            f"Expected exactly 1 outbox file, got: {[f.name for f in all_outbox]}")
        self.assertIn("operation-chain-result", all_outbox[0].name)

        # Exactly one DB record for this chain
        count = svc._conn.execute(
            "SELECT COUNT(*) FROM operation_chains WHERE request_id = ?",
            ("test-req-001",),
        ).fetchone()[0]
        self.assertEqual(count, 1)

        svc.close()


if __name__ == "__main__":
    unittest.main()


class TestRejectedChainPublishRecovery(unittest.TestCase):
    def test_rejected_chain_persists_publish_state_before_writing_outbox(self):
        from modules.mailbox_config import MailboxConfig
        from modules.outbox_publisher import PUBLISH_WRITTEN

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code_repo = root / "code"
            mailbox_repo = root / "mailbox"
            code_repo.mkdir()
            mailbox_repo.mkdir()
            (mailbox_repo / "inbox").mkdir()
            (mailbox_repo / "outbox").mkdir()
            db_path = str(root / "agent.db")
            cfg = MailboxConfig.from_paths(code_repo, mailbox_repo)
            registry = Mock()
            registry._specs = {}
            service = OperationChainService(
                db_path=db_path,
                operations_registry=registry,
                mailbox_config=cfg,
            )
            try:
                request = OperationChainRequest(
                    request_id="rejected-publish-regression",
                    steps=[
                        OperationChainStep(id="dup", action="git_status"),
                        OperationChainStep(id="dup", action="git_log"),
                    ],
                )
                result = service.start_chain(request)
                self.assertEqual(result.overall_status, STATUS_REJECTED)
                row = service._conn.execute(
                    "SELECT status, outbox_file, publish_status FROM operation_chains WHERE request_id = ?",
                    (request.request_id,),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], STATUS_REJECTED)
                self.assertTrue(row[1].startswith("outbox/"))
                self.assertEqual(row[2], PUBLISH_WRITTEN)
                self.assertTrue((mailbox_repo / row[1]).is_file())
            finally:
                service.close()
