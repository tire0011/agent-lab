"""#794: a verification test that runs longer than the limit writes its stack
once, so a slow VM test shows what it waits for."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from modules.verification_runner import _enable_slow_test_stacks, slow_test_stacks_path


class SlowTestStackTests(unittest.TestCase):
    def test_a_slow_test_dumps_its_stack_and_a_fast_one_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "test_sample.py").write_text(textwrap.dedent("""\
                import time, unittest
                class T(unittest.TestCase):
                    def test_fast(self):
                        pass
                    def test_waits_on_something(self):
                        time.sleep(1.5)
                """))
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            log = Path(tmp, "test-run1.jsonl")
            _enable_slow_test_stacks(env, log)
            env["NEXUS_SLOW_TEST_SECONDS"] = "0.5"
            subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", tmp, "-q"],
                           cwd=tmp, env=env, capture_output=True, text=True, timeout=60, check=True)
            stacks = slow_test_stacks_path(log).read_text()
            self.assertIn("test_waits_on_something", stacks)
            self.assertNotIn("test_fast", stacks)

    def test_without_the_variables_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.pop("NEXUS_SLOW_TEST_STACKS", None)
            done = subprocess.run([sys.executable, "-c", "import unittest; print(unittest.TestCase.run.__name__)"],
                                  env=env, capture_output=True, text=True, check=True)
            self.assertEqual(done.stdout.strip(), "run")
            self.assertEqual(os.listdir(tmp), [])


if __name__ == "__main__":
    unittest.main()
