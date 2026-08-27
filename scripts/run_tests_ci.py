from __future__ import annotations

import re
import subprocess
import sys


TEST_COMMAND = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"]
_SEPARATOR = re.compile(r"^=+$")
_HEADER = re.compile(r"^(FAIL|ERROR): ")
_SUMMARY = re.compile(r"^(Ran \d+ tests? in .*|FAILED \(.*\)|OK(?: \(skipped=.*\))?)$")


def _failure_blocks(output: str) -> list[str]:
    lines = output.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        if _HEADER.match(lines[i]):
            start = i
            if i > 0 and _SEPARATOR.match(lines[i - 1]):
                start = i - 1
            j = i + 1
            while j < len(lines):
                if _HEADER.match(lines[j]) or _SUMMARY.match(lines[j]):
                    break
                if j + 1 < len(lines) and _SEPARATOR.match(lines[j]) and _HEADER.match(lines[j + 1]):
                    break
                j += 1
            blocks.append("\n".join(lines[start:j]).strip())
            i = j
            continue
        i += 1
    return [block for block in blocks if block]


def main() -> int:
    proc = subprocess.run(
        TEST_COMMAND,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    output = proc.stdout or ""
    summaries = [line for line in output.splitlines() if _SUMMARY.match(line)]

    if proc.returncode == 0:
        print("\n".join(summaries[-2:]) or "OK")
        return 0

    blocks = _failure_blocks(output)
    if blocks:
        print("\n\n".join(blocks))
    else:
        print(output[-12000:])
    if summaries:
        print("\n" + "\n".join(summaries[-2:]))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
