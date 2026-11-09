#!/usr/bin/env python3
"""One-time migration of open Nexus todos/problems into GitHub Issues (#41).

Dry run by default: prints what would be created and what is skipped as a
duplicate. ``--apply`` creates the Issues through the GitHub MCP server.
Running it again creates nothing that already exists. See docs/github-mcp.md.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from modules import github_issue_migration as migration  # noqa: E402
from modules.github_mcp import GitHubMcpConfig, GitHubMcpError, open_session  # noqa: E402
from modules.run_store import DEFAULT_DB_PATH  # noqa: E402


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="Create the Issues (default: dry run).")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Experiment database (default: %(default)s).")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="KEY",
        help="Skip a candidate by its nexus-migration key (repeatable), e.g. one already done.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Only list local candidates; do not contact GitHub.",
    )
    return parser.parse_args(argv)


def _local_items(db_path: str) -> list[migration.MigrationItem]:
    import data_sources

    proposals = _REPO_ROOT / "docs" / "ideas-to-issues.md"
    return migration.collect_items(
        db_path=db_path,
        ideas=data_sources.list_ideas(),
        proposals_markdown=proposals.read_text(encoding="utf-8") if proposals.is_file() else "",
    )


async def _run(args: argparse.Namespace) -> int:
    items = [item for item in _local_items(args.db) if item.key not in set(args.exclude)]
    if args.offline:
        for item in items:
            print(f"[candidate] ({item.kind}) {item.title} - {item.key} - {item.source}")
        print(f"summary: {len(items)} local candidates")
        return 0
    async with open_session(GitHubMcpConfig.from_env()) as session:
        tools = {tool.name: tool for tool in await session.list_tools()}
        decisions = migration.plan(items, await migration.existing_issues(session, tools))
        if args.apply:
            await migration.apply(session, tools, decisions)
    print(migration.report(decisions))
    if not args.apply:
        print("dry run: nothing was created; rerun with --apply")
    return 1 if any(decision.action == "failed" for decision in decisions) else 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_run(_parse_args(argv)))
    except GitHubMcpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
