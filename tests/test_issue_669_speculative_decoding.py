"""Speculative decoding with a small draft model (#669)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from modules.llama_cpp_client import normalize_timings
from modules.local_model_rates import LocalModelRates, draft_from_timings
from modules.local_run_metrics import RunMetrics, call_record

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "scripts" / "setup-llama-systemd.sh"
START = ROOT / "scripts" / "nexus-llama-start.sh"
FETCH = ROOT / "scripts" / "fetch-draft-model.sh"
DRAFT = "qwen2.5-coder-0.5b-instruct-q8_0.gguf"
PINNED_SHA = "e1a77721fa97d412f121878223eec81fb4ae6f271e18f922d746711f67b344d1"

sys.path.insert(0, str(ROOT / "scripts"))
import bench_local_speed  # noqa: E402


def _fake(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + body + "\n")
    path.chmod(0o755)


class SetupExecStartTests(unittest.TestCase):
    """setup-llama-systemd.sh -> llama.env -> nexus-llama-start = the ExecStart."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.models = self.dir / "models"
        self.models.mkdir()
        (self.models / "qwen2.5-coder-7b-instruct-q4_k_m.gguf").write_text("main")
        self.conf = self.dir / "etc" / "llama.env"
        self.unit = self.dir / "llama-server.service"
        self.start_bin = self.dir / "nexus-llama-start"
        _fake(self.bin, "systemctl", "true")
        _fake(self.bin, "llama-server", 'echo "$@"')
        # install without root: -d makes directories, otherwise copy SRC DEST.
        _fake(self.bin, "install", (
            'd=0; args=()\n'
            'for a in "$@"; do case "$a" in -d) d=1;; -*) ;; *) args+=("$a");; esac; done\n'
            'if [[ $d == 1 ]]; then mkdir -p "${args[@]}"; else cp "${args[0]}" "${args[1]}"; chmod +x "${args[1]}"; fi'
        ))

    def _setup(self, expected=0, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith(("LLAMA_DRAFT_", "LOCAL_MODEL_"))}
        full = {
            **base, "PATH": f"{self.bin}:{os.environ['PATH']}",
            "LOCAL_MODEL_PATH": str(self.models), "LLAMA_SERVICE_FILE": str(self.unit),
            "NEXUS_LLAMA_CONF": str(self.conf), "NEXUS_LLAMA_START_BIN": str(self.start_bin),
            **env,
        }
        result = subprocess.run(["bash", str(SETUP)], env=full, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def _exec_start(self) -> list[str]:
        unit = self.unit.read_text()
        self.assertIn(f"ExecStart={self.start_bin}", unit)
        out = subprocess.run(
            ["bash", str(self.start_bin)], capture_output=True, text=True,
            env={**os.environ, "NEXUS_LLAMA_CONF": str(self.conf),
                 "LLAMA_SERVER_BIN": str(self.bin / "llama-server")})
        self.assertEqual(out.returncode, 0, out.stderr)
        return shlex.split(out.stdout)

    def test_without_draft_env_there_is_no_draft_flag(self):
        self._setup()
        args = self._exec_start()
        self.assertIn(str(self.models / "qwen2.5-coder-7b-instruct-q4_k_m.gguf"), args)
        self.assertNotIn("--model-draft", args)
        self.assertNotIn("DRAFT_", self.conf.read_text())

    def test_with_draft_env_and_file_the_flags_are_added(self):
        (self.models / DRAFT).write_text("draft")
        self._setup(LLAMA_DRAFT_MODEL_FILE=DRAFT)
        args = self._exec_start()
        i = args.index("--model-draft")
        self.assertEqual(args[i:i + 6], ["--model-draft", str(self.models / DRAFT),
                                         "--draft-max", "16", "--draft-min", "4"])

    def test_a_full_path_inside_the_model_dir_works(self):
        (self.models / DRAFT).write_text("draft")
        self._setup(LLAMA_DRAFT_MODEL_FILE=str(self.models / DRAFT))
        self.assertIn("--model-draft", self._exec_start())

    def test_a_missing_draft_file_gives_no_flag(self):
        result = self._setup(LLAMA_DRAFT_MODEL_FILE=DRAFT)
        self.assertIn("not found", result.stderr)
        self.assertNotIn("--model-draft", self._exec_start())
        self.assertNotIn("DRAFT_", self.conf.read_text())

    def test_a_draft_outside_the_model_dir_gives_no_flag(self):
        other = self.dir / DRAFT
        other.write_text("draft")
        self._setup(LLAMA_DRAFT_MODEL_FILE=str(other))
        self.assertNotIn("--model-draft", self._exec_start())

    def test_an_existing_conf_gets_the_draft_and_keeps_other_keys(self):
        self.conf.parent.mkdir(parents=True)
        self.conf.write_text(f"MODEL_DIR={self.models}\nMODEL=qwen2.5-coder-7b-instruct-q4_k_m.gguf\n"
                             "CTX=16384\nDRAFT_MAX=8\n")
        self._setup()  # no draft env: an existing conf is left as it is
        self.assertEqual(self.conf.read_text().count("DRAFT_MAX=8"), 1)
        (self.models / DRAFT).write_text("draft")
        self._setup(LLAMA_DRAFT_MODEL_FILE=DRAFT)
        conf = self.conf.read_text()
        self.assertIn("CTX=16384", conf)
        self.assertNotIn("DRAFT_MAX=8", conf)
        args = self._exec_start()
        self.assertIn("16384", args)
        self.assertEqual(args[args.index("--draft-max") + 1], "16")

    def test_explicit_empty_or_missing_draft_removes_preexisting_draft_keys(self):
        self.conf.parent.mkdir(parents=True)
        original = (f"MODEL_DIR={self.models}\nMODEL=qwen2.5-coder-7b-instruct-q4_k_m.gguf\n"
                    f"DRAFT_MODEL={DRAFT}\nDRAFT_MAX=8\nDRAFT_MIN=2\nCTX=8192\n")
        for setting in ("", "missing.gguf"):
            self.conf.write_text(original)
            result = self._setup(LLAMA_DRAFT_MODEL_FILE=setting)
            if setting:
                self.assertIn("not found", result.stderr)
            self.assertNotIn("DRAFT_", self.conf.read_text())
            self.assertNotIn("--model-draft", self._exec_start())

    def test_config_rewrite_is_atomic_and_safe_to_retry(self):
        self.conf.parent.mkdir(parents=True)
        original = (f"MODEL_DIR={self.models}\nMODEL=qwen2.5-coder-7b-instruct-q4_k_m.gguf\n"
                    f"DRAFT_MODEL={DRAFT}\nDRAFT_MAX=8\nDRAFT_MIN=2\nCTX=8192\n")
        self.conf.write_text(original)
        _fake(self.bin, "mv", "exit 77")
        self._setup(expected=77, LLAMA_DRAFT_MODEL_FILE="")
        self.assertEqual(self.conf.read_text(), original)
        self.assertEqual(list(self.conf.parent.glob("llama.env.tmp.*")), [])
        (self.bin / "mv").unlink()
        self._setup(LLAMA_DRAFT_MODEL_FILE="")
        first = self.conf.read_text()
        self._setup(LLAMA_DRAFT_MODEL_FILE="")
        self.assertEqual(self.conf.read_text(), first)
        self.assertNotIn("DRAFT_", first)

    def test_start_skips_a_draft_that_was_deleted_later(self):
        (self.models / DRAFT).write_text("draft")
        self._setup(LLAMA_DRAFT_MODEL_FILE=DRAFT)
        (self.models / DRAFT).unlink()
        self.assertNotIn("--model-draft", self._exec_start())


class FetchDraftModelTests(unittest.TestCase):
    CONTENT = b"fake gguf draft model\n"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.models = self.dir / "models"
        (self.dir / "payload").write_bytes(self.CONTENT)
        self.sha = hashlib.sha256(self.CONTENT).hexdigest()
        _fake(self.bin, "curl", (
            f'echo "curl $*" >> {self.dir}/calls.log\n'
            'case "$*" in\n'
            '  *-fsI*) printf "HTTP/2 302\\r\\nx-linked-etag: \\"${FAKE_ETAG}\\"\\r\\n";;\n'
            f'  *-o*) out=""; while [[ $# -gt 0 ]]; do [[ "$1" == -o ]] && out="$2"; shift; done; cp {self.dir}/payload "$out";;\n'
            'esac'
        ))

    def _run(self, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith("DRAFT_")}
        full = {**base, "PATH": f"{self.bin}:{os.environ['PATH']}",
                "LOCAL_MODEL_PATH": str(self.models), "FAKE_ETAG": self.sha, **env}
        return subprocess.run(["bash", str(FETCH)], env=full, capture_output=True, text=True)

    def test_default_download_uses_the_repository_pinned_sha256(self):
        _fake(self.bin, "sha256sum", f'printf "{PINNED_SHA}  %s\\n" "$1"')
        result = self._run(FAKE_ETAG="0" * 64)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.models / DRAFT).read_bytes(), self.CONTENT)
        calls = (self.dir / "calls.log").read_text()
        self.assertIn("Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF/resolve/main/" + DRAFT, calls)
        self.assertNotIn("-fsI", calls)
        self.assertIn(PINNED_SHA, result.stdout)

    def test_a_mismatch_deletes_the_download(self):
        result = self._run(DRAFT_MODEL_SHA256="0" * 64)
        self.assertEqual(result.returncode, 3)
        self.assertFalse((self.models / DRAFT).exists())
        self.assertEqual([p.name for p in self.models.iterdir()], [])

    def test_custom_artifact_without_explicit_sha_refuses_without_downloading(self):
        result = self._run(DRAFT_MODEL_URL="https://example.invalid/custom.gguf")
        self.assertEqual(result.returncode, 3)
        self.assertFalse((self.models / DRAFT).exists())
        self.assertFalse((self.dir / "calls.log").exists())

    def test_custom_artifact_accepts_an_explicit_sha_override(self):
        result = self._run(DRAFT_MODEL_URL="https://example.invalid/custom.gguf",
                           DRAFT_MODEL_SHA256=self.sha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.models / DRAFT).read_bytes(), self.CONTENT)

    def test_an_existing_file_is_verified_not_downloaded(self):
        self.models.mkdir()
        (self.models / DRAFT).write_bytes(self.CONTENT)
        self.assertEqual(self._run(DRAFT_MODEL_SHA256=self.sha.upper()).returncode, 0)
        self.assertFalse((self.dir / "calls.log").exists())
        (self.models / DRAFT).write_bytes(b"corrupt")
        self.assertEqual(self._run(DRAFT_MODEL_SHA256=self.sha).returncode, 3)

    def test_interrupted_download_is_resumable_and_rerun_is_idempotent(self):
        _fake(self.bin, "curl", (
            'out=""; while [[ $# -gt 0 ]]; do [[ "$1" == -o ]] && out="$2"; shift; done\n'
            'printf partial > "$out"\nexit 1'
        ))
        first = self._run(DRAFT_MODEL_SHA256=self.sha)
        self.assertEqual(first.returncode, 1)
        partial = self.models / f".{DRAFT}.part"
        self.assertTrue(partial.exists())
        self.assertFalse((self.models / DRAFT).exists())
        _fake(self.bin, "curl", (
            f'echo "$*" >> {self.dir}/calls.log\n'
            f'out=""; while [[ $# -gt 0 ]]; do [[ "$1" == -o ]] && out="$2"; shift; done; '
            f'cp {self.dir}/payload "$out"'
        ))
        self.assertEqual(self._run(DRAFT_MODEL_SHA256=self.sha).returncode, 0)
        self.assertIn("--continue-at -", (self.dir / "calls.log").read_text())
        (self.dir / "calls.log").unlink()
        self.assertEqual(self._run(DRAFT_MODEL_SHA256=self.sha).returncode, 0)
        self.assertFalse((self.dir / "calls.log").exists())


class DraftTimingsTests(unittest.TestCase):
    WITH_DRAFT = {"prompt_n": 100, "prompt_ms": 1000.0, "predicted_n": 50, "predicted_ms": 10000.0,
                  "draft_n": 40, "draft_n_accepted": 30}
    WITHOUT_DRAFT = {"prompt_n": 100, "prompt_ms": 1000.0, "predicted_n": 50, "predicted_ms": 10000.0}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "rates.json"

    def test_normalize_keeps_draft_counters_when_present(self):
        self.assertEqual(normalize_timings(self.WITH_DRAFT)["draft_n_accepted"], 30)
        self.assertNotIn("draft_n", normalize_timings(self.WITHOUT_DRAFT))

    def test_draft_from_timings(self):
        self.assertEqual(draft_from_timings(self.WITH_DRAFT),
                         {"draft_n": 40, "draft_n_accepted": 30, "draft_acceptance_rate": 0.75})
        for timings in (self.WITHOUT_DRAFT, None, {"draft_n": 0, "draft_n_accepted": 0},
                        {"draft_n": 5, "draft_n_accepted": 9}, {"draft_n": True, "draft_n_accepted": 1},
                        {"draft_n": 5}):
            self.assertEqual(draft_from_timings(timings), {}, timings)

    def test_rates_store_the_acceptance_rate_when_present(self):
        rates = LocalModelRates(self.path, model="m")
        self.assertTrue(rates.record(self.WITH_DRAFT))
        self.assertTrue(rates.record({**self.WITH_DRAFT, "draft_n_accepted": 0}))
        self.assertEqual(rates.draft_acceptance(), 0.375)  # median of 0.75 and 0.0
        stored = json.loads(self.path.read_text())["models"]["m"]
        self.assertEqual(stored["draft_acceptance_rate"], [0.75, 0.0])
        self.assertEqual(rates.medians(), (100.0, 5.0))

    def test_rates_ignore_missing_draft_counters(self):
        rates = LocalModelRates(self.path, model="m")
        self.assertTrue(rates.record(self.WITHOUT_DRAFT))
        self.assertIsNone(rates.draft_acceptance())
        self.assertFalse(json.loads(self.path.read_text())["models"]["m"].get("draft_acceptance_rate"))
        # Draft counters alone are no measurement.
        self.assertFalse(rates.record({"draft_n": 4, "draft_n_accepted": 2}))

    def test_call_record_and_run_metrics_carry_the_acceptance_rate(self):
        record = call_record("patch", "ok", 10.0, normalize_timings(self.WITH_DRAFT))
        self.assertEqual((record.draft_n, record.draft_n_accepted, record.draft_acceptance_rate),
                         (40, 30, 0.75))
        plain = call_record("patch", "ok", 10.0, normalize_timings(self.WITHOUT_DRAFT))
        self.assertIsNone(plain.draft_n)
        self.assertIsNone(plain.draft_acceptance_rate)
        metrics = RunMetrics(proc=Path("/nonexistent"))
        metrics.record_call(record)
        metrics.record_call(plain)
        fields = metrics.finish()
        self.assertEqual((fields["draft_n"], fields["draft_n_accepted"], fields["draft_acceptance_rate"]),
                         (40, 30, 0.75))
        empty = RunMetrics(proc=Path("/nonexistent"))
        empty.record_call(plain)
        self.assertIsNone(empty.finish()["draft_acceptance_rate"])


class _FakeLlama(BaseHTTPRequestHandler):
    timings: dict = {}
    requests: list = []

    def do_POST(self):  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, body))
        data = json.dumps({"choices": [{"message": {"content": "ok"}}], "timings": type(self).timings})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data.encode())

    def log_message(self, *_args):
        pass


class BenchLocalSpeedTests(unittest.TestCase):
    def setUp(self):
        _FakeLlama.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLlama)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bench_local_speed.main(["--url", *args])
        return code, json.loads(out.getvalue())

    def test_five_fixed_prompts_with_a_fixed_seed(self):
        prompts = bench_local_speed.fixed_prompts()
        self.assertEqual([p["kind"] for p in prompts], ["task"] * 4 + ["edit"])
        self.assertIn("<<<<<<< SEARCH", prompts[-1]["prompt"])
        self.assertNotIn("base_commit:", prompts[0]["prompt"])
        _FakeLlama.timings = DraftTimingsTests.WITHOUT_DRAFT
        code, report = self._main(self.url + "/v1", "--seed", "7")
        self.assertEqual(code, 0)
        self.assertEqual(len(_FakeLlama.requests), 5)
        for path, body in _FakeLlama.requests:
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual((body["seed"], body["temperature"], body["cache_prompt"]), (7, 0, False))
        self.assertEqual(report["summary"]["all"]["output_tokens_per_second"], 5.0)
        self.assertEqual(report["summary"]["all"]["prompt_tokens_per_second"], 100.0)
        self.assertIsNone(report["summary"]["all"]["acceptance_rate"])
        self.assertEqual(report["summary"]["edit"]["prompts"], 1)
        self.assertIn("mem_available_mib", report["memory"]["before"])

    def test_draft_run_reports_acceptance_and_speedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            _FakeLlama.timings = DraftTimingsTests.WITHOUT_DRAFT
            _code, baseline = self._main(self.url)
            path = Path(tmp) / "baseline.json"
            path.write_text(json.dumps(baseline))
            _FakeLlama.timings = {**DraftTimingsTests.WITH_DRAFT, "predicted_ms": 5000.0}
            code, report = self._main(self.url, "--baseline", str(path), "--label", "draft")
        self.assertEqual(code, 0)
        self.assertEqual(report["label"], "draft")
        self.assertEqual(report["results"][-1]["acceptance_rate"], 0.75)
        self.assertEqual(report["summary"]["edit"]["output_tokens_per_second"], 10.0)
        self.assertEqual(report["summary"]["all"]["draft_n"], 200)
        self.assertEqual(report["speedup"], {"all": 2.0, "edit": 2.0})

    def test_an_unreachable_server_is_reported_not_raised(self):
        self.server.shutdown()
        self.server.server_close()
        report = bench_local_speed.bench(self.url, timeout=2)
        self.assertEqual(report["summary"]["all"]["failed"], 5)
        self.assertIsNone(report["summary"]["all"]["output_tokens_per_second"])


if __name__ == "__main__":
    unittest.main()
