""""#845: review follow-up for #842 - canary report tamper and unblindable sampler.

The canary report path reaches the pytest child through
NEXUS_TEST_TIMING_CANARY_REPORT, so a repair patch naming that variable could
forge a canary report while skipping the flagged test. The tamper regex in
verification.py therefore rejects patches naming the canary report variable in
addition to the clock-drift marker. The in-call sampler in
DriftCanary._monitor_clocks (covered in tests/test_issue_842_drift_canary.py)
reads the clocks through the time module on every sample and paces itself with
a sleep captured before any test ran, so it cannot be blinded from test scope.
"""

from __future__ import annotations

import unittest

from modules import timing_check
from modules.experiment_service.service_parts import verification as verification_module


class TamperRegexTests(unittest.TestCase):
    def test_rejects_added_lines_naming_the_canary_report_env(self) -> None:
        regex = verification_module._TIMING_REPAIR_DRIFT_TAMPER
        patch = (
            "diff --git a/tests/test_new.py b/tests/test_new.py\n"
            "+++ b/tests/test_new.py\n"
            "@@ -1,1 +1,2 @@\n"
            "+    report = os.environ.get('NEXUS_TEST_TIMING_CANARY_REPORT')\n"
        )
        self.assertIsNotNone(regex.search(patch))

    def test_still_rejects_added_lines_naming_the_drift_marker(self) -> None:
        regex = verification_module._TIMING_REPAIR_DRIFT_TAMPER
        patch = "+# opt out when NEXUS_TEST_CLOCK_DRIFT_MS is set\n"
        self.assertIsNotNone(regex.search(patch))

    def test_markers_are_the_timing_check_module_constants(self) -> None:
        self.assertEqual(
            verification_module._TIMING_REPAIR_TAMPER_MARKERS,
            (timing_check.CLOCK_DRIFT_ENV, timing_check.CANARY_REPORT_ENV),
        )
        self.assertEqual(timing_check.CLOCK_DRIFT_ENV, "NEXUS_TEST_CLOCK_DRIFT_MS")
        self.assertEqual(timing_check.CANARY_REPORT_ENV, "NEXUS_TEST_TIMING_CANARY_REPORT")

    def test_ignores_removals_and_innocuous_additions(self) -> None:
        regex = verification_module._TIMING_REPAIR_DRIFT_TAMPER
        self.assertIsNone(regex.search("-    os.environ['NEXUS_TEST_TIMING_CANARY_REPORT']\n"))
        self.assertIsNone(regex.search("-# NEXUS_TEST_CLOCK_DRIFT_MS\n"))
        self.assertIsNone(regex.search("+import os\n+def test_budget():\n+    pass\n"))
        self.assertIsNone(regex.search("diff --git a/x b/x\n+++ b/x\n"))


if __name__ == "__main__":
    unittest.main()
