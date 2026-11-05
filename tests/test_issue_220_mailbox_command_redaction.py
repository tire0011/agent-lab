"""Issue #220: mailbox commands are accepted; secrets are redacted on output only.

Every credential below is a test fixture, never a real value.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from modules import mailbox
from modules.github_authorization import ActorDecision
from modules.mailbox import MailboxMessage, MailboxProcessor, _build_outbox_content
from modules.mailbox.development import install, process_command
from modules.redaction import redact_outbound, sensitive_environment_values

FIXTURE_KEY = "fixture-agent-api-key-7Hq2Zp9Lx"
FIXTURE_CUSTOM = "fixture-custom-service-token-Qm4"


def _base_env(tmp: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "NEXUS_MAILBOX_COMMAND_STATE_DIR": str(tmp / "jobs"),
        "AGENT_API_KEY": FIXTURE_KEY,
        "CUSTOM_SERVICE_TOKEN": FIXTURE_CUSTOM,
    }


def _run(msg, repo: Path, db_msg_id: int = 1):
    result = process_command(msg, repo, None, db_msg_id)
    deadline = time.monotonic() + 10
    while result is mailbox._DEFERRED_PROCESSING:
        if time.monotonic() >= deadline:
            raise AssertionError("command did not finish")
        time.sleep(0.02)
        result = process_command(msg, repo, None, db_msg_id)
    return result


class CommandInputIsNotScreenedTests(unittest.TestCase):
    def setUp(self):
        install(mailbox)
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name) / "inbox"
        self.inbox.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self, msg_type: str, body: str) -> MailboxMessage:
        path = self.inbox / f"{msg_type}.md"
        path.write_text(f"---\ntype: {msg_type}\ncreated_by: nexus\n---\n\n{body}\n")
        return MailboxMessage.load(path, self.inbox)

    def test_command_referencing_agent_api_key_is_accepted(self):
        body = 'curl -H "x-api-key: $AGENT_API_KEY" http://127.0.0.1:8000/secure-health'
        msg = self._load("command", body)
        self.assertEqual(msg.msg_type, "command")
        self.assertEqual(msg.body, body)

    def test_command_with_secret_like_words_is_accepted(self):
        for body in (
            "echo token api_key secret password",
            "API_KEY=$AGENT_API_KEY ./scripts/check.sh",
            "grep -n 'api-key: ' api.py",
            "AZURE_OPENAI_API_KEY=x python -c 'print(1)'",
            "echo sk-abcdefghijklmnop",
        ):
            with self.subTest(body=body):
                self.assertEqual(self._load("command", body).body, body)

    def test_other_message_types_are_still_screened(self):
        with self.assertRaisesRegex(ValueError, "Potential secret"):
            self._load("idea", "My API_KEY = supersecret123")

    def test_malformed_front_matter_with_secret_is_still_rejected(self):
        path = self.inbox / "bad.md"
        path.write_text("no front matter API_KEY = supersecret123")
        with self.assertRaisesRegex(ValueError, "Potential secret"):
            MailboxMessage.load(path, self.inbox)


class RedactOutboundTests(unittest.TestCase):
    env = {"AGENT_API_KEY": FIXTURE_KEY, "CUSTOM_SERVICE_TOKEN": FIXTURE_CUSTOM, "HOME": "/home/x"}

    def test_embedded_and_repeated_values_are_all_masked(self):
        text = (
            f"x-api-key: {FIXTURE_KEY}\n"
            f"GET http://127.0.0.1:8000/x?key={FIXTURE_KEY}&k2={FIXTURE_KEY}\n"
            f"prefix{FIXTURE_KEY}suffix and {FIXTURE_CUSTOM}"
        )
        out = redact_outbound(text, environ=self.env)
        self.assertNotIn(FIXTURE_KEY, out)
        self.assertNotIn(FIXTURE_CUSTOM, out)
        self.assertIn("x-api-key: ****", out)
        self.assertIn("prefix****suffix", out)
        self.assertEqual(out.count("****"), 5)

    def test_non_secret_output_is_unchanged(self):
        text = (
            "status: ok\n/home/agent/agent-lab/api.py:12 GET http://127.0.0.1:8000/health 200\n"
            "The GitHub token is not configured"
        )
        self.assertEqual(redact_outbound(text, environ=self.env), text)

    def test_generic_credential_shapes_are_masked_without_configuration(self):
        out = redact_outbound(
            "sk-abcdefghijklmnop ghp_abcdefghijklmnopqrstuv "
            "Authorization: Bearer abc.def https://user:pw@example.com/r password=hunter22",
            environ={},
        )
        for leaked in ("sk-abcdefghij", "ghp_abcdef", "abc.def", "user:pw", "hunter22"):
            self.assertNotIn(leaked, out)
        self.assertIn("Authorization: Bearer ****", out)

    def test_short_or_unrelated_values_are_not_treated_as_secrets(self):
        values = sensitive_environment_values({"API_KEY": "short", "HOME": "/home/agent/x"})
        self.assertEqual(values, [])


class ProcessCommandRedactionTests(unittest.TestCase):
    def test_secret_on_stdout_and_stderr_becomes_mask(self):
        msg = SimpleNamespace(
            body=f"printf 'x-api-key: {FIXTURE_KEY}\\n'; printf 'err {FIXTURE_CUSTOM}\\n' >&2",
            meta={"timeout": "5"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _base_env(Path(tmp)), clear=True):
                body, _ = _run(msg, Path(tmp))
        self.assertIn("**Exit code:** 0", body)
        self.assertIn("x-api-key: ****", body)
        self.assertIn("err ****", body)
        self.assertNotIn(FIXTURE_KEY, body)
        self.assertNotIn(FIXTURE_CUSTOM, body)

    def test_benign_output_is_unchanged(self):
        msg = SimpleNamespace(body="printf 'ready /home/agent/x http://127.0.0.1:8000/health\\n'", meta={})
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _base_env(Path(tmp)), clear=True):
                body, _ = _run(msg, Path(tmp))
        self.assertIn("ready /home/agent/x http://127.0.0.1:8000/health", body)

    def test_timeout_still_reports_and_masks_partial_output(self):
        msg = SimpleNamespace(body=f"printf '{FIXTURE_KEY}\\n' >&2; sleep 30", meta={"timeout": "1"})
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _base_env(Path(tmp)), clear=True):
                body, _ = _run(msg, Path(tmp))
        self.assertIn("**Exit code:** 124", body)
        self.assertIn("**Timed out:** yes", body)
        self.assertIn("[timeout] deadline 1s exceeded", body)
        self.assertNotIn(FIXTURE_KEY, body)

    def test_internal_synchronous_path_masks_too(self):
        msg = SimpleNamespace(body=f"printf 'k={FIXTURE_KEY}'; exit 3", meta={})
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _base_env(Path(tmp)), clear=True):
                body, _ = process_command(msg, Path(tmp), None, 0)
        self.assertIn("**Exit code:** 3", body)
        self.assertNotIn(FIXTURE_KEY, body)

    def test_invalid_timeout_is_still_rejected(self):
        with self.assertRaises(ValueError):
            process_command(SimpleNamespace(body="true", meta={"timeout": "0"}), Path("."), None, 0)


class OutboxNeverCarriesSecretTests(unittest.TestCase):
    """End to end: inbox command -> processor -> outbox file."""

    def setUp(self):
        install(mailbox)
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        for key, value in (("user.email", "nexus@example.invalid"), ("user.name", "Test")):
            subprocess.run(["git", "config", key, value], cwd=self.repo, check=True)
        for sub in ("inbox", "outbox", "state"):
            (self.repo / sub).mkdir()
        # Real writes and local commits (no remote, so the push is a no-op
        # failure): the test must see exactly what would leave the VM.
        self.processor = MailboxProcessor(
            repo_path=self.repo,
            db_path=str(self.repo / "state" / "agent.db"),
            dry_run=False,
            task_log_dir=self.repo / "state" / "logs",
        )
        self.env = patch.dict(os.environ, _base_env(self.repo), clear=True)
        self.env.start()
        self.trust = patch(
            "modules.github_authorization.classify_mailbox_message",
            return_value=ActorDecision(trusted=True, actor="tire0011"),
        )
        self.trust.start()

    def tearDown(self):
        self.trust.stop()
        self.env.stop()
        self.processor.close()
        self.tmp.cleanup()

    def _write(self, name: str, body: str) -> Path:
        path = self.repo / "inbox" / name
        path.write_text(f"---\ntype: command\ncreated_by: nexus\ntimeout: 10\n---\n\n{body}\n")
        return path

    def _drain(self, path: Path) -> dict:
        deadline = time.monotonic() + 10
        while True:
            result = self.processor.process_inbox([path])[0]
            if result["status"] != "pending":
                return result
            if time.monotonic() >= deadline:
                self.fail("command did not finish")
            time.sleep(0.05)

    def _all_outbox_text(self) -> str:
        files = list((self.repo / "outbox").glob("*.md"))
        self.assertTrue(files, "no outbox file was written")
        return "".join(p.read_text() for p in files)

    def _committed_text(self) -> str:
        return subprocess.run(
            ["git", "log", "-p", "--all"], cwd=self.repo, capture_output=True, text=True, check=True
        ).stdout

    def test_command_with_key_reference_runs_and_outbox_is_masked(self):
        # The command obtains the credential from the VM (here: a file) and
        # prints it, as a curl -v of an authenticated request would.
        (self.repo / "key.txt").write_text(FIXTURE_KEY)
        path = self._write(
            "20260918-1346-trace.md",
            'KEY="${AGENT_API_KEY:-$(cat key.txt)}"; '
            'printf "x-api-key: %s\\n" "$KEY"; printf "again %s %s\\n" "$KEY" "$KEY" >&2',
        )
        result = self._drain(path)
        self.assertEqual(result["status"], "done", result)
        outbox = self._all_outbox_text()
        self.assertIn("x-api-key: ****", outbox)
        self.assertIn("again **** ****", outbox)
        self.assertNotIn(FIXTURE_KEY, outbox)
        committed = self._committed_text()
        self.assertIn("x-api-key: ****", committed)
        self.assertNotIn(FIXTURE_KEY, committed)

    def test_command_input_quoting_the_secret_is_masked_in_outbox(self):
        path = self._write("echo.md", f"printf 'token=%s\\n' '{FIXTURE_KEY}'; echo plain-line")
        result = self._drain(path)
        self.assertEqual(result["status"], "done", result)
        outbox = self._all_outbox_text()
        self.assertIn("token=****", outbox)
        self.assertIn("plain-line", outbox)
        self.assertNotIn(FIXTURE_KEY, outbox)
        self.assertNotIn(FIXTURE_KEY, self._committed_text())

    def test_processor_error_text_is_masked_everywhere(self):
        def boom(*_args, **_kwargs):
            raise RuntimeError(f"failed with x-api-key: {FIXTURE_KEY}")

        path = self._write("boom.md", "true")
        with patch.dict(mailbox._PROCESSORS, {"command": boom}):
            result = self.processor.process_inbox([path])[0]
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(FIXTURE_KEY, result["error"])
        row = self.processor.conn.execute(
            "SELECT last_error FROM mailbox_messages WHERE file_path=?", (str(path),)
        ).fetchone()
        self.assertNotIn(FIXTURE_KEY, row[0])
        self.assertIn("****", row[0])
        notice = self.processor._write_discard_notice(path)
        self.assertNotIn(FIXTURE_KEY, notice.read_text())
        logs = "".join(
            p.read_text(errors="replace")
            for p in (self.repo / "state" / "logs").rglob("*")
            if p.is_file()
        )
        self.assertNotIn(FIXTURE_KEY, logs)

    def test_build_outbox_content_masks_command_documents(self):
        msg = SimpleNamespace(
            file_path=Path("inbox/x.md"), msg_type="command", created_by="nexus"
        )
        content = _build_outbox_content(msg, "done", "now", f"out {FIXTURE_KEY}")
        self.assertNotIn(FIXTURE_KEY, content)
        self.assertIn("out ****", content)


if __name__ == "__main__":
    unittest.main()
