"""Print the real rolling seven-day coverage and unresolved runs from a database."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from modules.run_store import init_db, run_cost_completeness, weekly_cost_by_provider
from modules.run_store.cost_recovery import recover_run_costs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", type=Path, help="Existing Nexus database")
    parser.add_argument("--recover-from", type=Path,
                        help="Apply a JSON list of verified evidence records before reporting")
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error(f"Database does not exist: {args.db}")

    if args.recover_from:
        evidence = json.loads(args.recover_from.read_text(encoding="utf-8"))
        if not isinstance(evidence, list):
            parser.error("Recovery evidence must be a list")
        with closing(init_db(str(args.db.resolve()))) as conn:
            recover_run_costs(conn, evidence)

    now = datetime.now(timezone.utc)
    with closing(sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)) as conn, conn:
        conn.row_factory = sqlite3.Row
        # Read all sections from the same snapshot even while workers are writing.
        conn.execute("BEGIN")
        coverage = run_cost_completeness(conn, now=now)
        unresolved = conn.execute(
            """SELECT id, timestamp, provider, model, run_kind, input_tokens, output_tokens,
                      CASE
                          WHEN error = 'Provider attempt did not finalize' THEN 'attempt did not finalize'
                          WHEN billing_evidence_json IS NOT NULL THEN 'captured evidence did not establish a cost'
                          WHEN input_tokens > 0 OR output_tokens > 0 THEN 'legacy aggregate counters only'
                          ELSE 'no billing evidence in the run store'
                      END AS reason
               FROM runs
               WHERE julianday(timestamp) BETWEEN julianday(?) AND julianday(?)
                 AND actual_cost IS NULL AND estimated_cost IS NULL AND cost_status != 'subscription'
               ORDER BY timestamp, id""",
            (coverage["since"], now.isoformat()),
        )
        print(json.dumps({
            "as_of": now.isoformat(),
            "coverage": coverage,
            "weekly_cost_by_provider_and_currency": weekly_cost_by_provider(conn, since=coverage["since"]),
            "unresolved_runs": [dict(row) for row in unresolved],
        }, indent=2))


if __name__ == "__main__":
    main()
