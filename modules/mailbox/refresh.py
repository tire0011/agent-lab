"""The `refresh` mailbox type: move the deployment to a branch, no shell (#228).

Everything the message can do is in the front matter (``type: refresh``,
``branch: main``); the body is ignored, so it cannot pick a repository, remote,
archive path, or service to restart - those come from the deployment, not from
whoever wrote the file. The operation's own safety is ``nexus_refresh``'s: rescue
a dirty checkout instead of discarding it, verify the checkout is really at
``origin/<branch>`` before restarting anything.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Services a refresh may restart - deliberately not reachable from the message:
# naming a unit would turn "move the checkout" into "restart anything on this host".
# Extending this list is a code change and a review.
ALLOWED_SERVICES: tuple[str, ...] = ("agent-api",)

MAX_BRANCH_CHARS = 200

# Allowlist of what a branch name may contain (not a denylist), so an
# unconsidered spelling is rejected. Nothing here ever reaches a shell - the
# refresh runs in-process - so this is defence in depth, not the only guard.
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,%d}$" % MAX_BRANCH_CHARS)


class InvalidBranchError(ValueError):
    """The message named something that is not usable as a branch."""


def validate_branch(raw: Any) -> str:
    """Return *raw* as a usable branch name, or say why it is not one."""
    branch = str(raw or "").strip()
    if not branch:
        raise InvalidBranchError("branch must not be empty")
    if len(branch) > MAX_BRANCH_CHARS:
        raise InvalidBranchError(f"branch must be at most {MAX_BRANCH_CHARS} characters")
    if not _BRANCH_PATTERN.match(branch):
        raise InvalidBranchError(
            "branch may only contain letters, digits, dot, underscore, slash and hyphen"
        )
    if ".." in branch:
        raise InvalidBranchError("branch must not contain '..'")
    if branch.startswith("-"):
        # Would be read as an option rather than a value by anything that ever
        # builds an argument list from it.
        raise InvalidBranchError("branch must not start with '-'")
    if branch.startswith("/") or branch.endswith("/") or "//" in branch:
        raise InvalidBranchError("branch must not start or end with '/' or contain '//'")
    if branch.endswith(".lock"):
        raise InvalidBranchError("branch must not end with '.lock'")
    return branch


def _default_refresher(branch: str, repo_root: Path) -> Any:
    from modules.nexus_refresh import NexusRefresh, systemd_restarter

    return NexusRefresh(
        repo_root,
        branch=branch,
        service_restarter=systemd_restarter(ALLOWED_SERVICES),
    ).run()


def process_refresh(
    msg: Any,
    repo_path: Path,
    conn: sqlite3.Connection,
    db_msg_id: int,
    *,
    refresher: Callable[[str, Path], Any] | None = None,
    repo_root: Path | str | None = None,
    sender_trusted: bool = True,
) -> tuple[str, None]:
    """Refresh the deployment checkout to the branch the message names - only a
    sender the authorization gate verified as trusted may request one."""
    del conn, db_msg_id

    if not sender_trusted:
        logger.warning("refresh rejected: sender is not a trusted actor")
        return (
            "## Refresh rejected\n\n"
            "**Reason:** the sender is not a trusted actor. "
            "A refresh needs approval by the repository owner.\n\n"
            "No refresh was attempted; the checkout is unchanged.",
            None,
        )

    try:
        branch = validate_branch((msg.meta or {}).get("branch", "main"))
    except InvalidBranchError as exc:
        logger.warning("refresh rejected: %s", exc)
        # Rejected before anything is touched; the reason goes back to the sender,
        # not only into a log they cannot read.
        return (
            f"## Refresh rejected\n\n"
            f"**Reason:** {exc}\n\n"
            f"No refresh was attempted; the checkout is unchanged.",
            None,
        )

    root = Path(repo_root) if repo_root is not None else _deployment_root()
    run = refresher or _default_refresher
    logger.info("refresh requested branch=%s repo_root=%s", branch, root)

    try:
        result = run(branch, root)
    except Exception as exc:  # noqa: BLE001 - the report is the deliverable
        # nexus_refresh reports its own failures rather than raising, so reaching here
        # means something further out broke; the sender still needs an answer, not a
        # traceback in a journal.
        logger.exception("refresh failed for branch %s", branch)
        return (
            f"## Refresh failed\n\n**Branch:** `{branch}`\n\n"
            f"**Error:** {str(exc)[:500]}",
            None,
        )

    return _format(branch, result), None


def _deployment_root() -> Path:
    from modules.nexus_refresh import DEFAULT_REPO_ROOT

    return Path(DEFAULT_REPO_ROOT)


def _format(branch: str, result: Any) -> str:
    """Render the refresh report: what it is on now, and whether it moved."""
    success = bool(getattr(result, "success", False))
    services = getattr(result, "services", None) or {}
    # A restart this process handed to systemd and will not survive to see is neither
    # done nor failed; calling it failed would misreport a restart that does happen.
    unknown_services = sorted(
        name for name, state in services.items() if "outcome unknown" in str(state)
    )
    scheduled_services = sorted(
        name
        for name, state in services.items()
        if str(state).startswith("restart ") and name not in unknown_services
    )
    failed_services = sorted(
        name
        for name, state in services.items()
        if str(state) != "restarted"
        and name not in scheduled_services
        and name not in unknown_services
    )
    if not success:
        heading = "failed"
    elif failed_services:
        # The checkout moved but the process did not: new code on disk, old code still
        # serving. Calling that "complete" is how a deploy gets believed before it
        # has happened.
        heading = "incomplete - code updated but a service did not restart"
    else:
        heading = "complete"
    lines = [
        "## Refresh " + heading,
        "",
        f"**Branch:** `{branch}`",
        f"**Success:** {success}",
    ]
    if failed_services:
        lines.append(
            f"**Not running the new code:** {', '.join(f'`{n}`' for n in failed_services)}"
        )
    if scheduled_services:
        lines.append(
            "**Restart scheduled:** "
            + ", ".join(f"`{n}`" for n in scheduled_services)
            + " (handed to systemd; this process is being replaced)"
        )
    if unknown_services:
        lines.append(
            "**Restart outcome unknown:** "
            + ", ".join(f"`{n}`" for n in unknown_services)
            + " (fallback attempted; verify the service is running the new code)"
        )
    previous = getattr(result, "previous_head", None)
    new_head = getattr(result, "new_head", None)
    if previous or new_head:
        lines.append(f"**Head:** `{previous or '?'}` -> `{new_head or '?'}`")
    lines.append(f"**Updated:** {bool(getattr(result, 'updated', False))}")

    if services:
        lines += ["", "### Services", ""]
        lines += [f"- `{name}`: {state}" for name, state in sorted(services.items())]
    else:
        # Said explicitly: "no services restarted" and "field missing" look the same
        # otherwise but mean different things after a successful refresh.
        lines += ["", "No services were restarted."]

    error = getattr(result, "error", None)
    if error:
        lines += ["", f"**Error:** {error}"]

    summary = getattr(result, "summary_lines", None)
    if callable(summary):
        try:
            rendered = [str(line) for line in summary()]
        except Exception:  # noqa: BLE001 - a report must not fail on its own prose
            rendered = []
        if rendered:
            lines += ["", "### Detail", "", "```text", *rendered, "```"]
    return "\n".join(lines)


def install(mailbox_module: Any) -> None:
    """Register `refresh` as a supported mailbox type."""
    mailbox_module.SUPPORTED_TYPES.add("refresh")
    mailbox_module._PROCESSORS["refresh"] = process_refresh
