"""Generic read-only Nexus diagnostics CLI."""

from __future__ import annotations

import argparse
import json

from modules.nexus_diagnostics import DiagnosticsError, NexusDiagnostics
from modules.run_store import DEFAULT_DB_PATH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nexus")
    sub = parser.add_subparsers(dest="command", required=True)

    debug = sub.add_parser("debug", help="Inspect persisted Nexus state read-only")
    debug.add_argument("reference", help="task/session/experiment/LLM-job id, issue/PR ref, or other supported reference")
    debug.add_argument(
        "--subject",
        default="auto",
        choices=("auto", "task", "experiment", "session", "llm_job"),
        help="force one diagnostic subject type instead of auto-resolving",
    )
    debug.add_argument(
        "--include",
        action="append",
        default=[],
        help="optional extra data to include; may be repeated (for example: prompt)",
    )
    debug.add_argument("--lines", type=int, default=None, help="maximum structured log lines")
    debug.add_argument("--db-path", default=DEFAULT_DB_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "debug":
        return 2

    try:
        with NexusDiagnostics(db_path=args.db_path) as diagnostics:
            payload = diagnostics.inspect(
                args.reference,
                subject=args.subject,
                include=args.include,
                lines=args.lines,
            )
    except DiagnosticsError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
