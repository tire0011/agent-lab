from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from modules.timing_check import affected_tests, run_timing_check


def test_affected_tests_finds_changed_tests_and_importers(tmp_path: Path):
    (tmp_path / "modules").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "modules" / "worker.py").write_text("VALUE = 1\n")
    (tmp_path / "tests" / "test_worker.py").write_text("from modules.worker import VALUE\n")
    (tmp_path / "tests" / "test_changed.py").write_text("def test_it(): pass\n")
    (tmp_path / "tests" / "test_other.py").write_text("import modules.other\n")
    diff = """diff --git a/modules/worker.py b/modules/worker.py
--- a/modules/worker.py
+++ b/modules/worker.py
diff --git a/tests/test_changed.py b/tests/test_changed.py
--- /dev/null
+++ b/tests/test_changed.py
"""
    assert affected_tests(diff, tmp_path) == ["tests/test_changed.py", "tests/test_worker.py"]


def test_reports_timing_sensitive_test_id(tmp_path: Path):
    test = tmp_path / "test_drift.py"
    test.write_text(
        "import os\n"
        "def test_budget():\n"
        "    assert 'NEXUS_TEST_CLOCK_DRIFT_MS' not in os.environ\n"
    )
    assert run_timing_check(tmp_path, ["test_drift.py"]) == "timing_sensitive: test_drift.py::test_budget"


def test_default_drift_is_50_and_there_is_no_file_cap(tmp_path: Path):
    seen = {}

    class Process:
        pid = 123
        returncode = 0

        def communicate(self): return ("", None)
        def poll(self): return 0

    def popen(command, **kwargs):
        seen.update(command=command, env=kwargs["env"])
        return Process()

    tests = [f"tests/test_{number}.py" for number in range(200)]
    with patch("modules.timing_check.subprocess.Popen", side_effect=popen):
        assert run_timing_check(tmp_path, tests) == "ok"
    assert seen["env"]["NEXUS_TEST_CLOCK_DRIFT_MS"] == "50"
    assert seen["command"][-200:] == tests


@pytest.mark.parametrize("interruption", [RuntimeError("observer failed"), KeyboardInterrupt()])
def test_callback_failure_and_keyboard_interrupt_close_child(tmp_path: Path, interruption: BaseException):
    events = []

    def callback(process):
        events.append(process)
        if process is not None:
            raise interruption

    if isinstance(interruption, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            run_timing_check(tmp_path, ["anything.py"], on_process=callback)
    else:
        assert run_timing_check(tmp_path, ["anything.py"], on_process=callback) == "error"
    child = events[0]
    assert child.poll() is not None
    assert events[-1] is None
