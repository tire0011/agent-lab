#!/usr/bin/env python3
"""Verify the GitHub MCP integration on this host (#41).

Without ``--write``: start the server, list its tools, list open Issues and
recent commits of GITHUB_REPOSITORY. With ``--write`` additionally create a
test Issue, comment on it, edit it and close it again.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from modules.github_mcp import GitHubMcpConfig, GitHubMcpError, open_session  # noqa: E402


def _check(label: str, result) -> object:
    if result.is_error:
        raise GitHubMcpError(f"{label} failed: {result.text[:300]}")
    print(f"ok: {label}")
    return result.json()


def _issues(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("issues") or payload.get("items") or []
    return []


async def _run(write: bool) -> None:
    config = GitHubMcpConfig.from_env()
    base = {"owner": config.owner, "repo": config.repo}
    async with open_session(config) as session:
        tools = {tool.name for tool in await session.list_tools()}
        print(f"ok: {len(tools)} tools from the GitHub MCP server")

        state = "OPEN"
        issues = _issues(_check("list open issues", await session.call_tool("list_issues", {**base, "state": state, "perPage": 5})))
        for issue in issues[:5]:
            print(f"  #{issue.get('number')} {issue.get('title')}")
        commits = _check("list commits", await session.call_tool("list_commits", {**base, "perPage": 3}))
        for commit in (commits if isinstance(commits, list) else [])[:3]:
            message = ((commit.get("commit") or {}).get("message") or "").splitlines()[:1]
            print(f"  {str(commit.get('sha'))[:8]} {message[0] if message else ''}")
        if not write:
            return

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        title = f"Nexus GitHub MCP check {stamp}"
        body = "Created by scripts/github_mcp_check.py (#41); closed again by the same run."
        if "issue_write" in tools:
            created = _check("create issue", await session.call_tool("issue_write", {**base, "method": "create", "title": title, "body": body}))
        else:
            created = _check("create issue", await session.call_tool("create_issue", {**base, "title": title, "body": body}))
        number = created.get("number") if isinstance(created, dict) else None
        if number is None and isinstance(created, dict):
            url = str(created.get("html_url") or created.get("url") or "")
            number = int(url.rstrip("/").rsplit("/", 1)[-1]) if url.rstrip("/").rsplit("/", 1)[-1].isdigit() else None
        if number is None:
            raise GitHubMcpError("create issue returned no issue number")
        print(f"  created #{number}")

        if "issue_read" in tools:
            _check("read issue", await session.call_tool("issue_read", {**base, "method": "get", "issue_number": number}))
        else:
            _check("read issue", await session.call_tool("get_issue", {**base, "issue_number": number}))
        _check("comment on issue", await session.call_tool("add_issue_comment", {**base, "issue_number": number, "body": "Comment from the GitHub MCP check."}))
        update = {**base, "issue_number": number}
        if "issue_write" in tools:
            _check("update issue", await session.call_tool("issue_write", {**update, "method": "update", "body": body + "\n\nUpdated."}))
            _check("close issue", await session.call_tool("issue_write", {**update, "method": "update", "state": "closed"}))
        else:
            _check("update issue", await session.call_tool("update_issue", {**update, "body": body + "\n\nUpdated."}))
            _check("close issue", await session.call_tool("update_issue", {**update, "state": "closed"}))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="Also create, comment, update and close a test Issue.")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_run(args.write))
    except GitHubMcpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
