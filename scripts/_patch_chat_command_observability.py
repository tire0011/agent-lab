from pathlib import Path

root = Path(__file__).resolve().parents[1]

# 1) Normalize a model-emitted final `action: command` back into the command tool.
path = root / "modules" / "task_orchestrator.py"
text = path.read_text(encoding="utf-8")
old = '''            decision = self._parse(raw)\n            if decision.get("type") == "tool":\n                observation = await self._execute_tool(decision)\n'''
new = '''            decision = self._parse(raw)\n            # Small local models occasionally emit the command capability with\n            # the final-action envelope (``action: command``) even though the\n            # prompt explicitly defines commands as tools. Treat that shape as\n            # the command tool instead of failing the whole persistent task.\n            if decision.get("type") != "tool" and str(decision.get("action", "")).strip().lower() == ACTION_COMMAND:\n                decision = dict(decision)\n                decision["type"] = "tool"\n                decision["tool"] = ACTION_COMMAND\n            if decision.get("type") == "tool":\n                observation = await self._execute_tool(decision)\n'''
if old not in text:
    raise SystemExit("task_orchestrator patch anchor not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8")

# 2) Add explicit lifecycle logs around async web-chat jobs.
path = root / "modules" / "nexus_chat_jobs.py"
text = path.read_text(encoding="utf-8")
old = '''        with self._lock:\n            self._jobs[job.id] = job\n        self._queue.put(job.id)\n        return self.get(job.id)  # type: ignore[return-value]\n'''
new = '''        with self._lock:\n            self._jobs[job.id] = job\n        logger.info(\n            "nexus_chat_job submitted job_id=%s session_id=%s message_chars=%s",\n            job.id, session_id or "-", len(text),\n        )\n        self._queue.put(job.id)\n        return self.get(job.id)  # type: ignore[return-value]\n'''
if old not in text:
    raise SystemExit("chat jobs submit anchor not found")
text = text.replace(old, new, 1)
old = '''            job.status = STATUS_RUNNING\n            message = job.message\n            session_id = job.session_id\n\n        try:\n'''
new = '''            job.status = STATUS_RUNNING\n            message = job.message\n            session_id = job.session_id\n\n        logger.info(\n            "nexus_chat_job running job_id=%s session_id=%s message_chars=%s",\n            job_id, session_id or "-", len(message),\n        )\n        try:\n'''
if old not in text:
    raise SystemExit("chat jobs running anchor not found")
text = text.replace(old, new, 1)
old = '''                    job.status = STATUS_FAILED\n                    job.error = f"{type(exc).__name__}: {exc}"\n            return\n\n        with self._lock:\n'''
new = '''                    job.status = STATUS_FAILED\n                    job.error = f"{type(exc).__name__}: {exc}"\n            logger.error(\n                "nexus_chat_job failed job_id=%s session_id=%s error_type=%s",\n                job_id, session_id or "-", type(exc).__name__,\n            )\n            return\n\n        with self._lock:\n'''
if old not in text:
    raise SystemExit("chat jobs failed anchor not found")
text = text.replace(old, new, 1)
old = '''                job.status = STATUS_SUCCEEDED\n                job.result = result\n                job.session_id = result.session_id\n'''
new = '''                job.status = STATUS_SUCCEEDED\n                job.result = result\n                job.session_id = result.session_id\n        logger.info(\n            "nexus_chat_job succeeded job_id=%s session_id=%s action=%s experiment_id=%s",\n            job_id, result.session_id, result.action, result.experiment_id or "-",\n        )\n'''
if old not in text:
    raise SystemExit("chat jobs success anchor not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8")

# 3) Regression test: action-envelope command is executed as a tool, then controller continues.
path = root / "tests" / "test_task_orchestrator.py"
text = path.read_text(encoding="utf-8")
marker = '''    def test_memory_is_a_tool_and_result_returns_to_controller(self):\n'''
insert = '''    def test_command_action_envelope_is_normalized_to_tool(self):\n        client = _Client([\n            '{"type":"action","action":"command","command":"git status --short",'\n            '"message":"","reason":"inspect repository"}',\n            '{"type":"action","action":"answer","experiment_id":null,"command":null,'\n            '"message":"Repository inspected.","reason":"command result is sufficient"}',\n        ])\n        orchestrator = self._orchestrator(client)\n        try:\n            from unittest.mock import patch\n            with patch(\n                "modules.task_orchestrator.process_command",\n                return_value=("exit_code: 0\\nstdout:\\nclean\\n", None),\n            ) as command_mock:\n                result = orchestrator.run("Inspect the repository")\n\n            self.assertEqual(result.action, ACTION_ANSWER)\n            command_mock.assert_called_once()\n            self.assertIn('"tool":"command"', client.prompts[1])\n            self.assertIn("clean", client.prompts[1])\n        finally:\n            orchestrator.close()\n\n'''
if marker not in text:
    raise SystemExit("task test anchor not found")
path.write_text(text.replace(marker, insert + marker, 1), encoding="utf-8")

# 4) Regression test for chat lifecycle logs.
path = root / "tests" / "test_nexus_chat_jobs.py"
text = path.read_text(encoding="utf-8")
marker = '''\n\nif __name__ == "__main__":\n'''
insert = '''\n    def test_job_lifecycle_is_logged(self):\n        service = NexusChatJobService(FakeChatService(), id_factory=lambda: "job-log")\n        self.addCleanup(service.close)\n        with self.assertLogs("modules.nexus_chat_jobs", level="INFO") as logs:\n            service.submit("hello", session_id="s-log")\n            deadline = time.time() + 2\n            while time.time() < deadline:\n                current = service.get("job-log")\n                if current and current.status == STATUS_SUCCEEDED:\n                    break\n                time.sleep(0.01)\n\n        combined = "\\n".join(logs.output)\n        self.assertIn("nexus_chat_job submitted job_id=job-log", combined)\n        self.assertIn("nexus_chat_job running job_id=job-log", combined)\n        self.assertIn("nexus_chat_job succeeded job_id=job-log", combined)\n'''
if marker not in text:
    raise SystemExit("chat jobs test anchor not found")
path.write_text(text.replace(marker, insert + marker, 1), encoding="utf-8")
