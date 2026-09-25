#!/usr/bin/env python3
import argparse
import sqlite3

def main() -> int:
    parser = argparse.ArgumentParser(description="Print the stored result for one local LLM job.")
    parser.add_argument("job_id")
    parser.add_argument("--db", default="/var/lib/nexus/agent.db")
    args = parser.parse_args()

    con = sqlite3.connect(args.db)
    row = con.execute(
        "SELECT result FROM local_llm_jobs WHERE id = ?",
        (args.job_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"job not found: {args.job_id}")

    print(row[0] or "")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
