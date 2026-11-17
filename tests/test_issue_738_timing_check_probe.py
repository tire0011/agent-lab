"""#738: old-vs-verification-environment timing probe report."""

from modules.timing_check import build_timing_check_env_probe_report


def test_probe_report_table_is_built_from_two_fake_result_sets():
    first = "tests/test_a.py::test_one"
    second = "tests/test_b.py::test_two"

    report = build_timing_check_env_probe_report(
        [first, second],
        f"timing_sensitive: {first}",
        f"timing_sensitive: {second}",
        {first: "old failure tail"},
        {second: "new failure tail"},
    )

    assert f"| `{first}` | FAIL | PASS |" in report
    assert f"| `{second}` | PASS | FAIL |" in report
    assert "### Old env failure tails" in report
    assert "old failure tail" in report
    assert "### Verification env failure tails" in report
    assert "new failure tail" in report
