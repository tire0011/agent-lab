"""Fail if any test file contributes no tests. CI ran ``unittest discover`` for months, which only collects
``unittest.TestCase`` subclasses, so files written as plain pytest functions/classes contributed zero tests
while CI reported green - including layers directly under the gate adoption treats as authoritative evidence.
The switch to pytest fixed that instance; this guards the class: a file contributing nothing is
indistinguishable from one whose tests all pass. Exits non-zero and names the offending files."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent.parent / "tests"

# Files that legitimately hold no tests of their own.
_EXEMPT = {"__init__.py", "conftest.py"}


def _collected_files(rootdir: Path) -> set[str]:
    """Test files pytest actually collected at least one test from."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(_TESTS_DIR)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(rootdir),
    )
    # 5 = no tests collected at all.
    if proc.returncode not in (0, 5):  # 5 = no tests collected at all
        print(proc.stdout[-4000:])
        print(proc.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"collection failed with exit code {proc.returncode}")

    found: set[str] = set()
    # Each collected item is reported as `tests/test_x.py::TestCase::test_name`.
    for line in proc.stdout.splitlines():
        match = re.match(r"^(\S+?\.py)::", line.strip())
        if match:
            found.add(Path(match.group(1)).name)
    return found


def main() -> int:
    rootdir = _TESTS_DIR.parent
    expected = {
        p.name
        for p in _TESTS_DIR.glob("test_*.py")
        if p.name not in _EXEMPT
    }
    if not expected:
        print("no test files found - is the tests directory correct?", file=sys.stderr)
        return 1

    collected = _collected_files(rootdir)
    empty = sorted(expected - collected)

    if empty:
        print("These test files contributed no tests:", file=sys.stderr)
        for name in empty:
            print(f"  tests/{name}", file=sys.stderr)
        print(
            "\nA file that collects nothing looks identical to a file that passes.\n"
            "Either it is not collectable (import error, wrong naming) or its\n"
            "tests are not discoverable by the configured runner.",
            file=sys.stderr,
        )
        return 1

    print(f"all {len(expected)} test files contribute at least one test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
