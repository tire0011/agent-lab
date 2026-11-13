"""Nexus may download and switch local models itself, with rollback (#673)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from modules.operations_registry import LLAMA_MODEL_COMMAND, OperationsRegistry, OperationValidationError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "nexus-llama-model.sh"
START = ROOT / "scripts" / "nexus-llama-start.sh"


class LlamaModelScriptTests(unittest.TestCase):
    """The root script against fake curl, systemctl and logger."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.models = self.dir / "models"
        self.models.mkdir()
        (self.models / "base.gguf").write_text("base")
        (self.models / "draft.gguf").write_text("draft")
        self.conf = self.dir / "llama.env"
        self.conf.write_text("MODEL_DIR=%s\nMODEL=base.gguf\nCTX=16384\nTHREADS=2\nPARALLEL=1\nPORT=8080\n" % self.models)
        self.calls = self.dir / "calls.log"
        self._fake("systemctl", f'echo "systemctl $*" >> {self.calls}')
        self._fake("logger", "true")
        self._fake("curl", self._curl_script())

    def _fake(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\n" + body + "\n")
        path.chmod(0o755)

    def _curl_script(self):
        # -fsIL: headers with a size; -fs .../health: healthy unless FAKE_UNHEALTHY;
        # -fL -o FILE URL: write a file.
        return (
            f'echo "curl $*" >> {self.calls}\n'
            'case "$*" in\n'
            '  *-fsIL*) printf "HTTP/2 200\\r\\ncontent-length: ${FAKE_SIZE:-100}\\r\\n";;\n'
            '  */health*) [[ -z "$FAKE_UNHEALTHY" ]];;\n'
            '  *-o*) out=""; while [[ $# -gt 0 ]]; do [[ "$1" == -o ]] && out="$2"; shift; done; echo data > "$out";;\n'
            'esac'
        )

    def _run(self, *args, **env):
        full = {
            **os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
            "NEXUS_LLAMA_CONF": str(self.conf), "NEXUS_LLAMA_MODEL_DIR": str(self.models),
            "NEXUS_LLAMA_HEALTH_WAIT": "2", "NEXUS_LLAMA_SPARE_BYTES": "0", **env,
        }
        return subprocess.run(["bash", str(SCRIPT), *args], env=full, capture_output=True, text=True)

    def test_download_writes_the_gguf(self):
        result = self._run("download", "Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF", "small.gguf")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.models / "small.gguf").exists())
        self.assertIn("https://huggingface.co/Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF/resolve/main/small.gguf",
                      self.calls.read_text())

    def test_download_refuses_bad_names_and_oversize(self):
        for args in (("x", "a.gguf"), ("a/b", "a.bin"), ("a/b", "../a.gguf"), ("a/b;rm", "a.gguf")):
            self.assertEqual(self._run("download", *args).returncode, 2, args)
        big = self._run("download", "a/b", "big.gguf", FAKE_SIZE="9000000000")
        self.assertEqual(big.returncode, 2)
        self.assertFalse((self.models / "big.gguf").exists())

    def test_switch_keeps_other_keys_and_restarts(self):
        result = self._run("switch", "DRAFT_MODEL=draft.gguf", "DRAFT_MAX=8")
        self.assertEqual(result.returncode, 0, result.stderr)
        conf = self.conf.read_text()
        self.assertIn("DRAFT_MODEL=draft.gguf", conf)
        self.assertIn("CTX=16384", conf)
        self.assertIn("systemctl restart llama-server", self.calls.read_text())

    def test_an_unhealthy_switch_is_rolled_back(self):
        before = self.conf.read_text()
        result = self._run("switch", "MODEL=draft.gguf", FAKE_UNHEALTHY="1")
        self.assertEqual(result.returncode, 4)
        self.assertEqual(self.conf.read_text(), before)
        self.assertEqual(self.calls.read_text().count("systemctl restart llama-server"), 2)

    def test_switch_refuses_unknown_keys_and_missing_models(self):
        for pair in ("MODEL=missing.gguf", "CTX=99", "EXTRA=--foo", "MODEL=../x.gguf"):
            self.assertEqual(self._run("switch", pair).returncode, 2, pair)
        self.assertNotIn("restart", self.calls.read_text() if self.calls.exists() else "")


class LlamaStartTests(unittest.TestCase):
    def test_start_builds_args_from_the_conf_without_sourcing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "llama.env"
            models = Path(tmp) / "m"
            models.mkdir()
            (models / "d.gguf").write_text("draft")  # #669: a missing draft gives no flag
            conf.write_text(f"MODEL_DIR={models}\nMODEL=a.gguf\nDRAFT_MODEL=d.gguf\nCTX=16384\n$(touch /tmp/x)=1\n")
            fake = Path(tmp) / "llama-server"
            fake.write_text('#!/usr/bin/env bash\necho "$@"\n')
            fake.chmod(0o755)
            out = subprocess.run(["bash", str(START)], capture_output=True, text=True,
                                 env={**os.environ, "NEXUS_LLAMA_CONF": str(conf), "LLAMA_SERVER_BIN": str(fake)})
        self.assertIn(f"--model {models}/a.gguf", out.stdout)
        self.assertIn("--ctx-size 16384", out.stdout)
        self.assertIn(f"--model-draft {models}/d.gguf", out.stdout)


class LlamaModelOperationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="ok\n", stderr=""))
        self.registry = OperationsRegistry(
            experiment_service=SimpleNamespace(), db_path=str(Path(tmp.name) / "agent.db"),
            repo_root=tmp.name, subprocess_runner=self.runner,
            github_work_tracking_factory=lambda **_kwargs: SimpleNamespace(),
        )
        self.addCleanup(self.registry.close)

    def test_download_and_switch_call_the_root_script(self):
        self.registry.execute("llama_model_download", {"repo": "Qwen/X-GGUF", "file": "x.gguf"})
        self.assertEqual(self.runner.call_args.args[0], [*LLAMA_MODEL_COMMAND, "download", "Qwen/X-GGUF", "x.gguf"])
        self.registry.execute("llama_model_switch", {"draft_model": "x.gguf", "draft_max": 8})
        self.assertEqual(self.runner.call_args.args[0],
                         [*LLAMA_MODEL_COMMAND, "switch", "DRAFT_MODEL=x.gguf", "DRAFT_MAX=8"])

    def test_bad_arguments_never_reach_the_script(self):
        for op, args in (("llama_model_download", {"repo": "a", "file": "x.gguf"}),
                         ("llama_model_download", {"repo": "a/b", "file": "x.bin"}),
                         ("llama_model_switch", {}),
                         ("llama_model_switch", {"extra": "--x"}),
                         ("llama_model_switch", {"model": "a b.gguf"})):
            with self.assertRaises(OperationValidationError):
                self.registry.execute(op, args)
        self.runner.assert_not_called()

    def test_switch_is_refused_while_an_experiment_runs(self):
        self.registry._conn.execute(
            "INSERT INTO experiments (id, goal, status, created_at) VALUES ('e1', 'g', 'implementing', 'now')")
        self.registry._conn.commit()
        with self.assertRaises(OperationValidationError):
            self.registry.execute("llama_model_switch", {"ctx": 8192})
        self.runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
