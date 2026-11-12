"""Architecture guard for the centralized experiment launch path (#660)."""

from pathlib import Path


def test_create_experiment_calls_are_limited_to_approved_modules():
    modules = Path(__file__).resolve().parents[1] / "modules"
    callers = {
        path.relative_to(modules).as_posix()
        for path in modules.rglob("*.py")
        if "create_experiment(" in path.read_text(encoding="utf-8")
    }

    assert callers == {
        "benchmark.py",
        "experiment_launcher.py",
        "experiment_service/service_parts/records.py",
    }
