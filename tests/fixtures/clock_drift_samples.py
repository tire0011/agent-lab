import time


drifted_monotonic = None


def test_elapsed_budget_uses_process_clock():
    started = time.monotonic()
    assert time.monotonic() - started < 0.025


def test_elapsed_budget_with_injected_clock():
    readings = iter((10.0, 10.01))
    clock = lambda: next(readings)

    started = clock()
    assert clock() - started < 0.025


def test_1_remember_drifted_clock():
    global drifted_monotonic
    drifted_monotonic = time.monotonic


def test_2_drift_patch_was_undone(real_clock):
    assert drifted_monotonic is not real_clock.monotonic
    assert time.monotonic is real_clock.monotonic
