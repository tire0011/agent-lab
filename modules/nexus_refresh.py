"""Non-destructive refresh of the live Nexus checkout.

Refresh used to be a shell sequence that gave up the moment ``/agent`` was
dirty, which left the VM running nine-commit-old code because of a single
generated outbox file.  This module drives the same deployment step as a
sequence that first *makes* the checkout deployable:

1. inspect branch, staged/tracked/untracked changes and ahead/behind state;
2. hand a dirty checkout to :class:`~modules.checkout_rescue.CheckoutRescue`,
   which commits work onto a rescue branch and archives runtime artifacts
   outside the repository - and aborts the refresh, untouched, if it cannot;
3. switch to the deployment branch and verify the worktree is clean;
4. fetch and fast-forward only - never a merge, a reset or a force;
5. verify the checkout really is at ``origin/<branch>``;
6. restart services only after that verification passes;
7. report what was rescued, what was archived, which commits moved, and how
   the services came back.

The module is also the CLI the deployment scripts call
(``python -m modules.nexus_refresh --repo-root /agent``); it prints a human
summary and, with ``--json``, the machine-readable result.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from modules.checkout_rescue import (
    CheckoutRescue,
    RescueError,
    RescueReport,
    git,
    inspect_checkout,
)
from modules.redaction import redact_secrets

logger = logging.getLogger(__name__)

DEFAULT_REPO_ROOT = "/agent"
DEFAULT_BRANCH = "main"
DEFAULT_REMOTE = "origin"

_FETCH_TIMEOUT = 120
_SERVICE_TIMEOUT = 60
DEFAULT_SERVICES: tuple[str, ...] = ("agent-api", "nexus-mailbox")

#: Distinguishes "detect my unit" from an explicit "I am in no unit".
_UNSET: Any = object()


#: A refresh must never end in a traceback: git can time out or the checkout
#: can vanish underneath it, and either way the operator needs the report, not
#: a stack trace.
_REFRESH_ERRORS = (RescueError, subprocess.SubprocessError, OSError)


def _message(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    return redact_secrets(text).strip()[:400]


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Everything an operator needs to see after a refresh attempt."""

    success: bool
    branch: str
    previous_head: str | None = None
    new_head: str | None = None
    updated: bool = False
    rescue: RescueReport | None = None
    diverged_branch: str | None = None
    services: dict[str, str] | None = None
    steps: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "branch": self.branch,
            "previous_head": self.previous_head,
            "new_head": self.new_head,
            "updated": self.updated,
            "rescue": self.rescue.to_dict() if self.rescue else None,
            "diverged_branch": self.diverged_branch,
            "services": dict(self.services) if self.services else None,
            "steps": list(self.steps),
            "error": self.error,
        }

    def summary_lines(self) -> list[str]:
        lines = [
            f"refresh: {'ok' if self.success else 'FAILED'}",
            f"branch: {self.branch}",
            f"head: {(self.previous_head or '?')[:12]} -> {(self.new_head or '?')[:12]}"
            + ("" if self.updated else " (already current)"),
        ]
        rescue = self.rescue
        if rescue and rescue.performed:
            if rescue.rescue_branch:
                lines.append(
                    f"rescued work: {rescue.rescue_branch} @ {(rescue.rescue_commit or '?')[:12]}"
                )
                lines.extend(f"  preserved: {path}" for path in rescue.rescued_paths)
            for archived in rescue.archived:
                suffix = " (already archived)" if archived.deduplicated else ""
                lines.append(f"  archived: {archived.repo_path} -> {archived.archived_path}{suffix}")
        else:
            lines.append("rescued work: none (checkout was clean)")
        if self.diverged_branch:
            lines.append(f"local commits preserved on: {self.diverged_branch}")
        if self.services:
            for service, status in sorted(self.services.items()):
                lines.append(f"service {service}: {status}")
        if self.error:
            lines.append(f"error: {self.error}")
        return lines


def own_unit(cgroup_path: str = "/proc/self/cgroup") -> str | None:
    """The systemd unit this process runs inside, if it can be determined.

    Restarting one's own unit is not the same operation as restarting someone
    else's, and the difference has to be known before the command is built -
    see ``systemd_restarter``.
    """
    try:
        text = Path(cgroup_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for piece in reversed(text.replace("\n", "/").split("/")):
        name = piece.strip()
        if name.endswith(".service"):
            return name[: -len(".service")]
    return None


def systemd_restarter(
    services: Sequence[str] = DEFAULT_SERVICES,
    *,
    use_sudo: bool = True,
    current_unit: str | None = _UNSET,
) -> Callable[[], dict[str, str]]:
    """Restarter that asks systemd to restart *services*, one report per unit.

    Restarting the unit this process runs inside needs ``--no-block``. Without
    it systemd tears the caller down while it is still waiting for its own
    ``systemctl`` to return, so the call comes back non-zero with both streams
    empty - and a restart that *worked* is reported as a failure. That is
    exactly what happened the first time a mailbox refresh restarted
    ``agent-api`` from inside ``agent-api``: the journal shows the unit
    stopping and starting cleanly while the report said ``error:``.

    ``--no-block`` returns as soon as the job is queued, so the outcome is
    genuinely unknown to us. It is reported as requested rather than as done,
    because claiming either would be a guess.
    """
    unit = own_unit() if current_unit is _UNSET else current_unit

    def restart() -> dict[str, str]:
        results: dict[str, str] = {}
        for service in services:
            replacing_self = unit is not None and service == unit
            prefix = ["sudo"] if use_sudo else []
            if replacing_self:
                # Hand the restart to systemd as a transient unit of its own.
                #
                # `--no-block` was not enough, and the reason is where the
                # caller lives: stopping a unit kills its whole control group,
                # and our `systemctl` is a child of the process being stopped,
                # so it is in that group. Returning early is still a race
                # against our own teardown, and we lost it - the refresh
                # reported `exit -15`, SIGTERM, for a restart that had in fact
                # succeeded.
                #
                # A transient unit is owned by systemd, not by us, so stopping
                # our own unit cannot take it with us. The delay gives this
                # process time to finish writing its report first.
                command = prefix + [
                    "systemd-run",
                    "--collect",
                    "--on-active=2",
                    "systemctl",
                    "restart",
                    service,
                ]
            else:
                command = prefix + ["systemctl", "restart", service]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=_SERVICE_TIMEOUT,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                results[service] = f"error: {redact_secrets(str(exc))[:200]}"
                continue
            if completed.returncode == 0:
                results[service] = (
                    "restart scheduled (this process is being replaced)"
                    if replacing_self
                    else "restarted"
                )
            else:
                # The exit code always goes in. systemctl can fail writing
                # nothing at all to either stream - a refresh reported
                # "service agent-api: error: " with no reason after it, which
                # says only that something went wrong somewhere.
                detail = redact_secrets(completed.stderr or completed.stdout).strip()[:200]
                if not detail:
                    detail = "no output; run it by hand to see why"
                results[service] = f"error: exit {completed.returncode}: {detail}"
        return results

    return restart


class NexusRefresh:
    """Refresh a deployment checkout, self-healing a dirty working tree."""

    def __init__(
        self,
        repo_root: Path | str = DEFAULT_REPO_ROOT,
        *,
        branch: str = DEFAULT_BRANCH,
        remote: str = DEFAULT_REMOTE,
        archive_root: Path | str | None = None,
        extra_artifact_patterns: Iterable[str] | None = None,
        service_restarter: Callable[[], dict[str, str]] | None = None,
        rescue: CheckoutRescue | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._repo_root = Path(repo_root).resolve()
        self._branch = branch
        self._remote = remote
        self._service_restarter = service_restarter
        self._rescue = rescue or CheckoutRescue(
            self._repo_root,
            target_branch=branch,
            archive_root=archive_root,
            extra_artifact_patterns=extra_artifact_patterns,
            clock=clock,
        )

    @property
    def rescue(self) -> CheckoutRescue:
        return self._rescue

    def run(self) -> RefreshResult:
        steps: list[str] = []
        try:
            state = inspect_checkout(self._repo_root, remote=self._remote)
        except _REFRESH_ERRORS as exc:
            return RefreshResult(
                success=False, branch=self._branch, error=_message(exc), steps=("inspect",)
            )
        steps.append("inspect")

        report: RescueReport | None = None
        if not state.clean:
            try:
                report = self._rescue.prepare(state)
            except _REFRESH_ERRORS as exc:
                # Nothing was updated: rescue rolls itself back, so the
                # checkout is still exactly what the operator left behind.
                return RefreshResult(
                    success=False,
                    branch=state.branch,
                    previous_head=state.head,
                    new_head=state.head,
                    rescue=None,
                    steps=(*steps, "rescue-failed"),
                    error=f"refresh aborted before touching {self._branch}: {_message(exc)}",
                )
            steps.append("rescue")

        previous_head = state.head
        diverged_branch: str | None = None
        try:
            self._switch_to_branch()
            steps.append("switch")
            previous_head = self._head()
            self._fetch()
            steps.append("fetch")
            new_head, updated, diverged_branch = self._fast_forward(previous_head)
            steps.append("update")
            self._verify(new_head)
            steps.append("verify")
        except _REFRESH_ERRORS as exc:
            return RefreshResult(
                success=False,
                branch=self._branch,
                previous_head=previous_head,
                new_head=self._head_or_none(),
                updated=False,
                rescue=report,
                diverged_branch=diverged_branch,
                steps=(*steps, "failed"),
                error=_message(exc),
            )

        services: dict[str, str] | None = None
        if self._service_restarter is not None:
            services = self._service_restarter()
            steps.append("restart")

        return RefreshResult(
            success=True,
            branch=self._branch,
            previous_head=previous_head,
            new_head=new_head,
            updated=updated,
            rescue=report,
            diverged_branch=diverged_branch,
            services=services,
            steps=tuple(steps),
        )

    # -- steps -----------------------------------------------------------

    def _switch_to_branch(self) -> None:
        current = inspect_checkout(self._repo_root, remote=self._remote)
        if not current.clean:
            raise RescueError(
                f"{self._branch} is still dirty after rescue; refusing to update it"
            )
        if current.branch != self._branch or current.detached:
            result = git(self._repo_root, ["switch", self._branch])
            if result.returncode != 0:
                raise RescueError(
                    f"could not switch to {self._branch}: "
                    f"{redact_secrets(result.stderr).strip()[:300]}"
                )

    def _fetch(self) -> None:
        result = git(
            self._repo_root,
            ["fetch", self._remote, self._branch],
            timeout=_FETCH_TIMEOUT,
        )
        if result.returncode != 0:
            raise RescueError(
                f"could not fetch {self._remote}/{self._branch}: "
                f"{redact_secrets(result.stderr).strip()[:300]}"
            )

    def _fast_forward(self, previous_head: str) -> tuple[str, bool, str | None]:
        target = git(
            self._repo_root, ["rev-parse", "--verify", f"{self._remote}/{self._branch}"]
        )
        if target.returncode != 0:
            raise RescueError(f"{self._remote}/{self._branch} is unknown after fetch")
        target_sha = target.stdout.strip()
        if target_sha == previous_head:
            return previous_head, False, None

        diverged_branch: str | None = None
        ahead = git(
            self._repo_root,
            ["rev-list", "--count", f"{self._remote}/{self._branch}..HEAD"],
        )
        local_commits = int(ahead.stdout.strip() or 0) if ahead.returncode == 0 else 0
        if local_commits:
            # A branch carrying local commits cannot fast-forward.  Name the
            # commits first, then move the branch reference: no reset, no
            # force checkout, and every commit stays reachable by name.
            diverged_branch = self._rescue.preserve_commits(previous_head)
            self._realign_branch(target_sha, diverged_branch)
            return target_sha, True, diverged_branch

        merge = git(
            self._repo_root, ["merge", "--ff-only", f"{self._remote}/{self._branch}"]
        )
        if merge.returncode != 0:
            raise RescueError(
                f"fast-forward of {self._branch} failed; no merge or reset was performed: "
                f"{redact_secrets(merge.stderr or merge.stdout).strip()[:300]}"
            )
        return self._head(), True, diverged_branch

    def _realign_branch(self, target_sha: str, preserved_branch: str) -> None:
        detach = git(self._repo_root, ["switch", "--detach", target_sha])
        if detach.returncode != 0:
            raise RescueError(
                f"local commits are preserved on {preserved_branch}, but {self._branch} "
                f"could not be realigned: {redact_secrets(detach.stderr).strip()[:300]}"
            )
        move = git(self._repo_root, ["branch", "--force", self._branch, target_sha])
        if move.returncode != 0:
            git(self._repo_root, ["switch", self._branch])
            raise RescueError(
                f"local commits are preserved on {preserved_branch}, but {self._branch} "
                f"could not be moved: {redact_secrets(move.stderr).strip()[:300]}"
            )
        back = git(self._repo_root, ["switch", self._branch])
        if back.returncode != 0:
            raise RescueError(
                f"{self._branch} was updated, but the checkout is detached: "
                f"{redact_secrets(back.stderr).strip()[:300]}"
            )

    def _verify(self, expected_head: str) -> None:
        state = inspect_checkout(self._repo_root, remote=self._remote)
        problems: list[str] = []
        if state.detached or state.branch != self._branch:
            problems.append(f"checkout is on {state.branch}, expected {self._branch}")
        if not state.clean:
            problems.append("worktree is not clean")
        if state.head != expected_head:
            problems.append(f"HEAD is {state.head[:12]}, expected {expected_head[:12]}")
        if problems:
            raise RescueError("; ".join(problems))

    # -- helpers ---------------------------------------------------------

    def _head(self) -> str:
        result = git(self._repo_root, ["rev-parse", "HEAD"])
        if result.returncode != 0:
            raise RescueError("could not read HEAD")
        return result.stdout.strip()

    def _head_or_none(self) -> str | None:
        try:
            return self._head()
        except _REFRESH_ERRORS:
            return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexus-refresh",
        description="Refresh the live Nexus checkout, self-healing a dirty working tree.",
    )
    parser.add_argument("--repo-root", default=DEFAULT_REPO_ROOT, help="deployment checkout path")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="deployment branch")
    parser.add_argument("--remote", default=DEFAULT_REMOTE, help="git remote to refresh from")
    parser.add_argument(
        "--archive-root",
        default=None,
        help="directory for archived runtime artifacts (default: NEXUS_RUNTIME_ARCHIVE_DIR "
        "or /var/lib/nexus/runtime-archive)",
    )
    parser.add_argument(
        "--artifact-pattern",
        action="append",
        default=[],
        help="additional runtime-artifact glob, relative to the repository root",
    )
    parser.add_argument(
        "--restart-services",
        action="append",
        default=[],
        help="systemd unit to restart after a verified update (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument(
        "--json-out",
        default=None,
        help="also write the JSON result to this file, so a caller can report on it",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    restarter = (
        systemd_restarter(tuple(args.restart_services)) if args.restart_services else None
    )
    result = NexusRefresh(
        args.repo_root,
        branch=args.branch,
        remote=args.remote,
        archive_root=args.archive_root,
        extra_artifact_patterns=args.artifact_pattern,
        service_restarter=restarter,
    ).run()
    payload = json.dumps(result.to_dict(), indent=2, sort_keys=True)
    if args.json:
        print(payload)
    else:
        for line in result.summary_lines():
            print(line)
    if args.json_out:
        try:
            Path(args.json_out).write_text(payload, encoding="utf-8")
        except OSError as exc:
            print(f"could not write the refresh report: {exc}", file=sys.stderr)
    return 0 if result.success else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
