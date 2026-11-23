"""Mistral via the Vibe CLI: runs on the owner's Vibe Code subscription instead
of the API workspace that answered HTTP 402 (2026-11-20). A fake ``vibe``
records its argv and environment and writes export.json like the real CLI."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from modules import coding_provider
from modules.vibe_worker import PROVIDER, VibeWorker

FAKE_VIBE = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    args = sys.argv[1:]
    def opt(name):
        return args[args.index(name) + 1] if name in args else None
    record = {{"args": args, "has_mistral_key": "MISTRAL_API_KEY" in os.environ,
               "prompt": open(opt("--prompt-file"), encoding="utf-8").read()}}
    open(os.environ["FAKE_VIBE_RECORD"], "w").write(json.dumps(record))
    mode = os.environ.get("FAKE_VIBE_MODE", "ok")
    out = opt("--output-dir"); os.makedirs(out, exist_ok=True)
    export = {{"outcome": "finished", "error": None, "session_id": "s-1",
               "usage": {{"input_tokens": 120, "output_tokens": 30, "cached_input_tokens": 0}}}}
    if mode == "402":
        export.update(outcome="infrastructure_failure",
                      error={{"message": "402 Payment Required", "code": "backend_error"}})
        open(os.path.join(out, "export.json"), "w").write(json.dumps(export)); sys.exit(2)
    if mode == "timelimit":
        open(os.path.join(out, "export.json"), "w").write(json.dumps(dict(export, outcome="time_limit"))); sys.exit(3)
    if "--auto-approve" in args:
        open(os.path.join(opt("--workdir"), "edited.txt"), "w").write("vibe was here\\n")
    open(os.path.join(out, "export.json"), "w").write(json.dumps(export))
    # The real vibe 3.x journal entries (checked on the VM); --output streaming
    # prints one per line while the run works.
    entries = [
        {{"type": "message", "role": "user", "content": [{{"type": "text", "text": "Add edited.txt"}}]}},
        {{"type": "reasoning", "text": "simple"}},
        {{"type": "effect"}},
        {{"type": "message", "role": "assistant", "content": [{{"type": "text", "text": "done: "}},
                                                             {{"type": "text", "text": "edited.txt"}}]}},
    ]
    if opt("--output") == "streaming":
        for entry in entries:
            print(json.dumps(entry), flush=True)
    else:
        print(json.dumps(entries))
''')


class VibeWorkerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.workdir = self.root / "wt"
        self.workdir.mkdir()
        self.vibe = self.root / "vibe"
        self.vibe.write_text(FAKE_VIBE.format(python=sys.executable), encoding="utf-8")
        self.vibe.chmod(self.vibe.stat().st_mode | stat.S_IEXEC)
        self.record = self.root / "record.json"
        env = {"FAKE_VIBE_RECORD": str(self.record), "MISTRAL_API_KEY": "api-workspace-key"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        # filtered_env only passes known keys: let the fake see its record path.
        keys = mock.patch.object(VibeWorker, "_EXTRA_ENV_KEYS",
                                 VibeWorker._EXTRA_ENV_KEYS | {"FAKE_VIBE_RECORD", "FAKE_VIBE_MODE",
                                                               "MISTRAL_API_KEY"})
        keys.start()
        self.addCleanup(keys.stop)

    def run_worker(self, sandbox="workspace-write", **kwargs):
        return VibeWorker(vibe_bin=str(self.vibe)).run("Add edited.txt", str(self.workdir), sandbox=sandbox, **kwargs)

    def recorded(self):
        return json.loads(self.record.read_text())

    def test_workspace_write_run_edits_and_reports_usage(self):
        result = self.run_worker()
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.provider, PROVIDER)
        self.assertEqual(result.final_message, "done: edited.txt")
        self.assertEqual(result.usage["input_tokens"], 120)
        self.assertEqual(result.thread_id, "s-1")
        self.assertTrue((self.workdir / "edited.txt").exists())
        args = self.recorded()["args"]
        self.assertEqual(args[args.index("--output") + 1], "streaming")
        for flag in ("--prompt-file", "--output", "--output-dir", "--trust", "--auto-approve", "--time-limit"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--workdir") + 1], str(self.workdir))

    def test_the_api_workspace_key_is_never_passed_to_vibe(self):
        self.run_worker()
        self.assertFalse(self.recorded()["has_mistral_key"])

    def test_read_only_enables_only_read_tools(self):
        result = self.run_worker(sandbox="read-only")
        self.assertTrue(result.success)
        args = self.recorded()["args"]
        self.assertNotIn("--auto-approve", args)
        tools = [args[i + 1] for i, a in enumerate(args) if a == "--enabled-tools"]
        self.assertEqual(tools, ["read_file", "grep"])
        self.assertFalse((self.workdir / "edited.txt").exists())

    def test_payment_error_is_reported_and_counts_as_a_provider_limit(self):
        with mock.patch.dict(os.environ, {"FAKE_VIBE_MODE": "402"}):
            result = self.run_worker()
        self.assertFalse(result.success)
        self.assertIn("402 Payment Required", result.error)
        self.assertTrue(coding_provider.is_provider_limit(result.error))

    def test_time_limit_exit_is_a_timeout(self):
        with mock.patch.dict(os.environ, {"FAKE_VIBE_MODE": "timelimit"}):
            result = self.run_worker()
        self.assertFalse(result.success)
        self.assertTrue(result.timed_out)

    def test_output_schema_is_added_to_the_prompt(self):
        self.run_worker(output_schema={"type": "object"})
        self.assertIn('{"type": "object"}', self.recorded()["prompt"])

    def test_missing_binary_is_a_clean_failure(self):
        result = VibeWorker(vibe_bin=str(self.root / "nope")).run("x", str(self.workdir))
        self.assertFalse(result.success)
        self.assertIn("not found", result.error)


class StreamingProgressTests(unittest.TestCase):
    def test_a_watched_run_sees_output_lines_while_vibe_works(self):
        """The stall watchdog counts output lines; streaming must produce them."""
        from modules.vibe_worker import _final_message
        lines = "\n".join(json.dumps(e) for e in [
            {"type": "message", "role": "user", "content": [{"type": "text", "text": "q"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "pong"}]},
        ])
        self.assertEqual(_final_message(lines), "pong")
        self.assertEqual(_final_message("not json at all"), "not json at all")


class BinaryLookupTests(unittest.TestCase):
    def test_falls_back_to_the_service_users_local_bin_when_not_on_path(self):
        from modules import vibe_worker
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "vibe"
            fake.write_text("#!/bin/sh\n", encoding="utf-8")
            fake.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if k != "AGENT_VIBE_BIN"}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(vibe_worker, "_SERVICE_USER_VIBE", str(fake)), \
                    mock.patch("modules.vibe_worker.shutil.which",
                               side_effect=lambda name: str(fake) if name == str(fake) else None):
                self.assertEqual(vibe_worker.default_vibe_bin(), str(fake))
                self.assertTrue(vibe_worker.vibe_available())


class ProviderSelectionTests(unittest.TestCase):
    def test_mistral_uses_vibe_when_the_cli_is_installed(self):
        with mock.patch("modules.coding_provider.vibe_available", return_value=True):
            self.assertIsInstance(coding_provider.build_worker("mistral"), VibeWorker)
            self.assertTrue(coding_provider.is_available("mistral"))

    def test_mistral_falls_back_to_the_api_worker_without_the_cli(self):
        with mock.patch("modules.coding_provider.vibe_available", return_value=False):
            worker = coding_provider.build_worker("mistral")
        self.assertNotIsInstance(worker, VibeWorker)
        self.assertEqual(worker.provider, "mistral")


if __name__ == "__main__":
    unittest.main()
