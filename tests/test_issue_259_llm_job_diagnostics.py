"""Read-only diagnostics for one persisted local LLM job (#259).

``nexus_llm_job`` is the operations-catalog form of
``nexus debug llm-job <id> [--result|--prompt]``: one exact job id, the
stored metadata by default, the full result or prompt on request, secrets
masked, and the database left untouched.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from modules.operations_registry import OperationsRegistry
from tests.test_nexus_diagnostics import DiagnosticsFixture

_SECRET = "sk-" + "a" * 40


class LlmJobFixture(DiagnosticsFixture):
    def setUp(self) -> None:
        super().setUp()
        # Same shape LocalLlmJobService creates; built by hand so no worker
        # thread starts and tries to reach llama-server.
        self.conn.execute(
            """
            CREATE TABLE local_llm_jobs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, prompt TEXT NOT NULL,
                temperature REAL NOT NULL, max_tokens INTEGER NOT NULL,
                response_format TEXT, session_id TEXT, created_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT, result TEXT, error TEXT,
                role TEXT NOT NULL DEFAULT 'controller', system TEXT, error_kind TEXT,
                worker_id TEXT, lease_expires_at TEXT
            )
            """
        )
        self.conn.commit()

    def add_job(self, **fields) -> str:
        job_id = uuid.uuid4().hex
        row = {
            "id": job_id,
            "status": "succeeded",
            "prompt": f"Plan the next step. api_key={_SECRET}",
            "temperature": 0.0,
            "max_tokens": 1024,
            "response_format": '{"type":"json_object"}',
            "session_id": "sess-1",
            "created_at": "2026-09-26T06:00:00+00:00",
            "started_at": "2026-09-26T06:00:01+00:00",
            "finished_at": "2026-09-26T06:00:43.500000+00:00",
            "result": '{"action": "finish"}',
            "error": None,
            "role": "controller",
            "system": "You are the Nexus controller.",
            **fields,
        }
        columns = ", ".join(row)
        self.conn.execute(
            f"INSERT INTO local_llm_jobs ({columns}) VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )
        self.conn.commit()
        return job_id

    def registry(self) -> OperationsRegistry:
        class _ExperimentService:
            experiment_log_dir = self.log_dir

        registry = OperationsRegistry(
            experiment_service=_ExperimentService(),
            db_path=self.db_path,
            repo_root=str(self.root),
        )
        self.addCleanup(registry.close)
        return registry

    def run_op(self, **args):
        return self.registry().execute("nexus_llm_job", args)


class LlmJobDiagnosticsTests(LlmJobFixture):
    def test_summary_shows_metadata_and_masks_secrets(self) -> None:
        job_id = self.add_job()
        result = self.run_op(job_id=job_id)
        self.assertTrue(result.success)
        parsed = dict(line.split(": ", 1) for line in result.output_lines if ": " in line)
        self.assertEqual(job_id, parsed["job_id"])
        self.assertEqual("controller", parsed["role"])
        self.assertEqual("sess-1", parsed["session_id"])
        self.assertEqual("local", parsed["provider"])
        self.assertEqual("succeeded", parsed["status"])
        self.assertEqual("42.5s", parsed["duration"])
        self.assertEqual('{"action": "finish"}', parsed["result_preview"])
        self.assertNotEqual("0", parsed["prompt_chars"])
        self.assertNotIn(_SECRET, "\n".join(result.output_lines))

    def test_result_view_returns_the_full_result(self) -> None:
        long_result = "\n".join(f"line {index}" for index in range(200))
        job_id = self.add_job(result=long_result)
        result = self.run_op(job_id=job_id, view="result")
        self.assertTrue(result.success)
        self.assertEqual(f"job_id: {job_id}", result.output_lines[0])
        self.assertEqual("result:", result.output_lines[1])
        self.assertEqual(long_result.splitlines(), result.output_lines[2:])

    def test_prompt_view_returns_system_and_prompt_masked(self) -> None:
        job_id = self.add_job()
        result = self.run_op(job_id=job_id, view="prompt")
        self.assertTrue(result.success)
        text = "\n".join(result.output_lines)
        self.assertIn("You are the Nexus controller.", text)
        self.assertIn("Plan the next step.", text)
        self.assertNotIn(_SECRET, text)

    def test_failed_job_reports_its_error(self) -> None:
        job_id = self.add_job(
            status="failed", result=None, error="llama-server timed out", error_kind="timeout"
        )
        parsed = dict(
            line.split(": ", 1) for line in self.run_op(job_id=job_id).output_lines
        )
        self.assertEqual("timeout", parsed["error_kind"])
        self.assertEqual("llama-server timed out", parsed["error"])
        self.assertEqual("-", parsed["result_preview"])

    def test_missing_job_is_a_clear_finding(self) -> None:
        self.add_job()
        result = self.run_op(job_id="does-not-exist")
        self.assertFalse(result.success)
        self.assertIn("no local LLM job", result.error_summary or "")
        self.assertIn("does-not-exist", result.error_summary or "")

    def test_prefix_does_not_match(self) -> None:
        job_id = self.add_job()
        self.assertFalse(self.run_op(job_id=job_id[:8]).success)

    def test_lookup_does_not_modify_the_database(self) -> None:
        job_id = self.add_job()
        self.conn.close()
        before = hashlib.sha256(Path(self.db_path).read_bytes()).hexdigest()
        from modules.nexus_diagnostics import NexusDiagnostics

        with NexusDiagnostics(db_path=self.db_path, log_dir=self.log_dir) as diagnostics:
            diagnostics.llm_job(job_id)
        after = hashlib.sha256(Path(self.db_path).read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_is_catalogued_read_only(self) -> None:
        catalog = {spec.name: spec for spec in self.registry().catalog()}
        self.assertTrue(catalog["nexus_llm_job"].read_only)


class LlmJobDiagnosticsApiTests(LlmJobFixture):
    def setUp(self) -> None:
        super().setUp()
        import os

        os.environ.setdefault("AGENT_API_KEY", "test-key")
        import api
        from fastapi.testclient import TestClient
        from modules.nexus_diagnostics import NexusDiagnostics

        diagnostics = NexusDiagnostics(db_path=self.db_path, log_dir=self.log_dir)
        self.addCleanup(diagnostics.close)
        api.app.dependency_overrides[api.get_nexus_diagnostics] = lambda: diagnostics
        self.addCleanup(api.app.dependency_overrides.clear)
        self.client = TestClient(api.app)
        self.headers = {"x-api-key": api.API_KEY}

    def test_route_returns_plain_text_views(self) -> None:
        job_id = self.add_job()
        summary = self.client.get(f"/diagnostics/llm-jobs/{job_id}", headers=self.headers)
        self.assertEqual(200, summary.status_code)
        self.assertIn("role: controller", summary.text)
        result = self.client.get(
            f"/diagnostics/llm-jobs/{job_id}?view=result", headers=self.headers
        )
        self.assertIn('{"action": "finish"}', result.text)
        prompt = self.client.get(
            f"/diagnostics/llm-jobs/{job_id}?view=prompt", headers=self.headers
        )
        self.assertIn("Plan the next step.", prompt.text)
        self.assertNotIn(_SECRET, prompt.text)

    def test_route_404_for_missing_job_and_requires_key(self) -> None:
        self.assertEqual(
            404, self.client.get("/diagnostics/llm-jobs/nope", headers=self.headers).status_code
        )
        self.assertEqual(401, self.client.get("/diagnostics/llm-jobs/nope").status_code)


class LlmJobMaskingTests(LlmJobFixture):
    """Credentials are masked; paths, URLs and JSON structure stay intact."""

    def test_paths_urls_and_json_stay_readable(self) -> None:
        prompt = (
            'Edit /home/agent/repo/modules/x.py, see https://github.com/o/r/issues/259. '
            '{"action": "edit", "token_budget": 800}'
        )
        job_id = self.add_job(prompt=prompt, result="wrote /tmp/work/out.json")
        text = "\n".join(self.run_op(job_id=job_id, view="prompt").output_lines)
        self.assertIn(prompt, text)
        result = "\n".join(self.run_op(job_id=job_id, view="result").output_lines)
        self.assertIn("/tmp/work/out.json", result)

    def test_credentials_are_still_masked(self) -> None:
        ghp = "ghp_" + "b" * 36
        prompt = (
            f"key {_SECRET} gh {ghp} Authorization: Bearer abc.def "
            "clone https://user:hunter2@github.com/o/r.git"
        )
        job_id = self.add_job(prompt=prompt)
        text = "\n".join(self.run_op(job_id=job_id, view="prompt").output_lines)
        for secret in (_SECRET, ghp, "abc.def", "hunter2"):
            self.assertNotIn(secret, text)
        self.assertIn("github.com/o/r.git", text)

    def test_generic_credential_assignments_are_masked(self) -> None:
        prompt = (
            "password=hunter2 api_key=some-private-value token: abc123def456 "
            '{"password":"json-secret", "action": "edit"}'
        )
        job_id = self.add_job(prompt=prompt, result=prompt)
        for view in ("prompt", "result"):
            with self.subTest(view=view):
                text = "\n".join(self.run_op(job_id=job_id, view=view).output_lines)
                for secret in ("hunter2", "some-private-value", "abc123def456", "json-secret"):
                    self.assertNotIn(secret, text)
                self.assertIn('"action": "edit"', text)


class LlmJobCliTests(LlmJobFixture):
    """``nexus debug llm-job <id> [--result|--prompt]`` (scripts/nexus.ps1 debug)."""

    def test_argument_mapping(self) -> None:
        from modules.nexus_diagnostics import build_cli_parser

        parser = build_cli_parser()
        for argv, view in (
            (["debug", "llm-job", "abc"], "summary"),
            (["debug", "llm-job", "abc", "--result"], "result"),
            (["debug", "llm-job", "abc", "--prompt"], "prompt"),
        ):
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertEqual(("abc", view), (args.job_id, args.view))
        with self.assertRaises(SystemExit):
            parser.parse_args(["debug", "llm-job", "abc", "--result", "--prompt"])

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        import contextlib
        import io

        from modules.nexus_diagnostics import main

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["debug", "llm-job", *argv, "--db", self.db_path])
        return code, out.getvalue(), err.getvalue()

    def test_cli_prints_each_view(self) -> None:
        job_id = self.add_job()
        code, out, _ = self.run_cli(job_id)
        self.assertEqual(0, code)
        self.assertIn("role: controller", out)
        self.assertIn('{"action": "finish"}', self.run_cli(job_id, "--result")[1])
        prompt = self.run_cli(job_id, "--prompt")[1]
        self.assertIn("Plan the next step.", prompt)
        self.assertNotIn(_SECRET, prompt)

    def test_cli_missing_job_exits_nonzero(self) -> None:
        code, out, err = self.run_cli("does-not-exist")
        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertIn("no local LLM job", err)
