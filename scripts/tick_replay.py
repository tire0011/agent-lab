"""Counterfactual tick replay (#755): paid starts a policy would have saved.

    python scripts/tick_replay.py --since 2026-11-01 --policy provider_cooldown,issue_loop_guard
    python scripts/tick_replay.py --since 2026-11-01 --json > replay.json

Reads the run store read-only (``--db``, default ``AGENT_DB_PATH``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import tick_replay, tick_replay_store  # noqa: E402
from modules.run_store import DEFAULT_DB_PATH  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Nexus SQLite database (opened read-only)")
    parser.add_argument("--since", default=None, help="ISO date or time (default: 30 days ago)")
    parser.add_argument("--until", default=None, help="ISO date or time (default: now)")
    parser.add_argument("--policy", default=None,
                        help=f"comma-separated policies (default: all of {','.join(tick_replay.POLICIES)})")
    parser.add_argument("--history", choices=tick_replay.HISTORY_MODES, default="actual",
                        help="what the policies see: every real row, or only the allowed ones")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    try:
        report, seconds = tick_replay_store.report_from_db(
            args.db, args.since, args.policy, until=args.until, history=args.history,
        )
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
    if args.json:
        data = report.to_dict()
        data["seconds"] = round(seconds, 3)
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(tick_replay.format_report(report))
        print(f"replayed in {seconds:.2f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
