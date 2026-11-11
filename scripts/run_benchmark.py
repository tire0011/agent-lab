#!/usr/bin/env python3
"""Run the fixed experiment benchmark and compare it to a baseline (#343).

See benchmarks/README.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from modules.benchmark import (  # noqa: E402
    BenchmarkError,
    BenchmarkRunner,
    compare_runs,
    default_benchmark_db_path,
    format_report,
    format_test_first_report,
    get_baseline,
    load_tasks,
    set_baseline,
)
from modules.coding_provider import KNOWN_PROVIDERS  # noqa: E402
from modules.run_store import init_db  # noqa: E402


def _run(args: argparse.Namespace) -> int:
    prompt_file = Path(args.prompt_file) if args.prompt_file else None
    runner = BenchmarkRunner(
        repo_root=args.repo_root,
        db_path=args.db,
        test_python_executable=args.test_python,
        provider=args.provider,
        model=args.model,
        prompt_name=prompt_file.stem if prompt_file else "default",
        prompt_text=prompt_file.read_text(encoding="utf-8").strip() if prompt_file else "",
        test_first=None if args.test_first is None else args.test_first == "on",
    )
    print(runner.run(load_tasks(only=args.task), run_id=args.run_id))
    return 0


def _baseline(args: argparse.Namespace) -> int:
    conn = init_db(args.db)
    try:
        set_baseline(conn, args.run_id)
    finally:
        conn.close()
    return 0


def _report(args: argparse.Namespace) -> int:
    conn = init_db(args.db)
    try:
        baseline = args.baseline or get_baseline(conn)
        if baseline is None:
            print("no baseline stored; use the baseline command or --baseline", file=sys.stderr)
            return 2
        print(format_report(compare_runs(conn, args.run_id, baseline)), end="")
    finally:
        conn.close()
    return 0


def _test_first_report(args: argparse.Namespace) -> int:
    conn = init_db(args.db)
    try:
        print(format_test_first_report(conn, args.run_on, args.run_off), end="")
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=default_benchmark_db_path())
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run the task set; prints the run id")
    run.add_argument("--provider", choices=KNOWN_PROVIDERS, help="default: the normal rotation")
    run.add_argument("--model", help="needs --provider")
    run.add_argument("--prompt-file", help="appended to every coding-agent call")
    run.add_argument("--task", action="append", help="only this task id; repeatable")
    run.add_argument("--run-id")
    run.add_argument("--repo-root", default=str(_REPO_ROOT))
    run.add_argument("--test-python", default=sys.executable)
    run.add_argument(
        "--test-first", choices=("on", "off"),
        help="send every task to the local model first, with or without generated tests (#636)",
    )
    run.set_defaults(handler=_run)

    baseline = commands.add_parser("baseline", help="store a run as the baseline")
    baseline.add_argument("run_id")
    baseline.set_defaults(handler=_baseline)

    report = commands.add_parser("report", help="put a run next to the baseline")
    report.add_argument("run_id")
    report.add_argument("--baseline", help="default: the stored baseline")
    report.set_defaults(handler=_report)

    test_first = commands.add_parser(
        "test-first-report", help="put a test-first on run next to an off run (#636)"
    )
    test_first.add_argument("run_on")
    test_first.add_argument("run_off")
    test_first.set_defaults(handler=_test_first_report)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (BenchmarkError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
