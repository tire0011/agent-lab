"""Loaded by verification test runs (PYTHONPATH): a test still running after
NEXUS_SLOW_TEST_SECONDS writes all thread stacks once to the file named by
NEXUS_SLOW_TEST_STACKS, so a slow test shows what it waits for (#794: tests
on the VM took 30-50 s each while using almost no CPU). The stack names the
test method, so no header is written. Does nothing without both variables."""

import os

_path = os.environ.get("NEXUS_SLOW_TEST_STACKS")
_limit = os.environ.get("NEXUS_SLOW_TEST_SECONDS")
if _path and _limit:
    try:
        import faulthandler
        import unittest

        _seconds = float(_limit)
        _out = open(_path, "a", encoding="utf-8")  # noqa: SIM115 - lives as long as the run
        _orig_run = unittest.TestCase.run

        def _run(self, result=None):
            faulthandler.dump_traceback_later(_seconds, repeat=False, file=_out)
            try:
                return _orig_run(self, result)
            finally:
                faulthandler.cancel_dump_traceback_later()

        unittest.TestCase.run = _run
    except Exception:  # noqa: BLE001 - diagnostics must never break a test run
        pass
