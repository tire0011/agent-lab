"""A stand-in for github/github-mcp-server, spoken to over real stdio (#41).

Implements the handful of tool names Nexus relies on, with in-memory Issues.
Not a test module itself; tests/test_github_mcp.py starts it as a subprocess.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server import MCPServer

server = MCPServer("fake-github", log_level="WARNING")
_ISSUES: dict[int, dict[str, Any]] = {
    1: {"number": 1, "title": "Existing open issue", "state": "OPEN", "body": "x"},
}


@server.tool()
def get_me() -> dict[str, Any]:
    """Who the token belongs to."""
    return {"login": "nexus", "token_seen": os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN") == "test-token"}


@server.tool()
def list_issues(owner: str, repo: str, state: str = "OPEN", perPage: int = 30, after: str | None = None) -> dict[str, Any]:
    """List issues in a repository."""
    issues = [issue for issue in _ISSUES.values() if issue["state"] == state.upper()]
    return {"issues": issues, "pageInfo": {"hasNextPage": False, "endCursor": None}}


@server.tool()
def issue_write(
    method: str,
    owner: str,
    repo: str,
    title: str | None = None,
    body: str | None = None,
    issue_number: int | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """Create or update an issue."""
    if method == "create":
        number = max(_ISSUES) + 1
        _ISSUES[number] = {"number": number, "title": title, "state": "OPEN", "body": body or ""}
        return {"number": number, "url": f"https://github.com/{owner}/{repo}/issues/{number}"}
    issue = _ISSUES[int(issue_number)]
    if state:
        issue["state"] = state.upper()
    if body is not None:
        issue["body"] = body
    return {"number": issue["number"], "state": issue["state"]}


@server.tool()
def add_issue_comment(owner: str, repo: str, issue_number: int, body: str) -> dict[str, Any]:
    """Comment on an issue."""
    if int(issue_number) not in _ISSUES:
        raise ValueError("no such issue")
    return {"issue_number": issue_number, "body": body}


@server.tool()
def list_commits(owner: str, repo: str, perPage: int = 30) -> list[dict[str, Any]]:
    """List commits."""
    return [{"sha": "abc1234def", "commit": {"message": "Initial commit"}}]


if __name__ == "__main__":
    server.run("stdio")
