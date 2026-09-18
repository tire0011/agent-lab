from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modules.central_logging import DEFAULT_LOG_DIR
from .config import MailboxConfig, MailboxDrainLimits


logger = logging.getLogger(__name__)


@dataclass
class MailboxPollRunResult:
    status: str
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


_TERMINAL_VALIDATION_ERROR_PREFIXES = (
    "Missing YAML front matter",
    "Unclosed YAML front matter",
    "Invalid front-matter line",
    "Missing required front-matter field",
    "Unknown message type",
    "File too large",
    "Binary content is not allowed",
    "Symlinks are not allowed",
    "Path traversal detected",
)


def _safe_result_summary(results: list[dict[str, Any]], limit: int = 20) -> str:
    """Return bounded, content-free metadata for mailbox result logging."""
    items: list[str] = []
    for result in results[:limit]:
        status = str(result.get("status") or "?")[:32]
        file_value = result.get("file")
        filename = Path(str(file_value)).name[:160] if file_value else "?"
        items.append(f"{status}:{filename}")
    if len(results) > limit:
        items.append(f"+{len(results) - limit}_more")
    return ",".join(items) or "none"


def _is_terminal_validation_error(error: str | None) -> bool:
    if not error:
        return False
    return error.startswith(_TERMINAL_VALIDATION_ERROR_PREFIXES)


def _finalize_terminal_validation_errors(
    *,
    processor: Any,
    results: list[dict[str, Any]],
    mailbox_module: Any,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Turn deterministic inbox validation failures into rejected outbox replies.

    Parser/schema failures cannot succeed on retry without changing the source
    message.  They are therefore terminal: produce a bounded/redacted outbox
    response, mark the DB row done, and finalize the normal inbox/outbox git
    commit.  Runtime/processing failures are deliberately left untouched so the
    existing retry policy still applies to transient failures.
    """
    for result in results:
        if result.get("status") != "failed":
            continue
        error = str(result.get("error") or "")
        if not _is_terminal_validation_error(error):
            continue

        file_value = result.get("file")
        if not file_value:
            continue
        file_path = processor._mailbox_repo_path / str(file_value)
        existing = processor._get_existing_record(file_path)
        if not existing:
            continue

        processed_at = mailbox_module._now_iso()
        synthetic_msg = mailbox_module.MailboxMessage(
            file_path=file_path,
            msg_type="unknown",
            created_by="unknown",
            meta={},
            body="",
        )
        outbox_name = mailbox_module._outbox_filename(synthetic_msg, processed_at)
        outbox_path = processor.outbox / outbox_name
        safe_error = mailbox_module.redact(error)[:500]
        outbox_content = mailbox_module._build_outbox_content(
            synthetic_msg,
            "rejected",
            processed_at,
            f"Mailbox message rejected: {safe_error}",
            None,
        )

        if dry_run:
            result.update(status="rejected", outbox=outbox_name)
            continue

        mailbox_module._atomic_write(outbox_path, outbox_content)
        processor.conn.execute(
            """
            UPDATE mailbox_messages
            SET status=?, processed_at=?, outbox_file=?, last_error=?
            WHERE id=?
            """,
            (
                mailbox_module.STATUS_DONE_PENDING_COMMIT,
                processed_at,
                outbox_name,
                safe_error,
                existing["id"],
            ),
        )
        processor.conn.commit()
        processor._finalize_git_commit(
            {
                "id": existing["id"],
                "file_path": file_path,
                "msg_type": "unknown",
                "created_by": "unknown",
                "outbox_file": outbox_name,
                "status": mailbox_module.STATUS_DONE_PENDING_COMMIT,
            }
        )
        result.update(status="rejected", outbox=outbox_name)

    return results


def _worktree_safe_for_mailbox_sync(
    mailbox_module: Any,
    repo_path: Path,
    ignored_untracked_prefixes: tuple[str, ...] = (),
) -> bool:
    """Return whether mailbox Git operations can proceed safely.

    Untracked files below explicitly supplied machine-managed prefixes (normally
    the outbox directory) are tolerated.  The publisher commits exact pathspecs,
    so these files cannot be swept into unrelated commits.  Staged changes,
    modified tracked files, and untracked files elsewhere remain blocking.
    """
    status = mailbox_module._git(["status", "--porcelain"], repo_path, check=False)
    if status.returncode != 0:
        return False

    prefixes = tuple(prefix.rstrip("/") + "/" for prefix in ignored_untracked_prefixes)
    for entry in status.stdout.splitlines():
        if not entry.strip():
            continue
        if entry.startswith("?? "):
            path = entry[3:].strip('\"')
            if any(path.startswith(prefix) for prefix in prefixes):
                continue
        return False
    return True


def _sync_mailbox_repo(
    mailbox_module: Any,
    repo_path: Path,
    max_attempts: int = 3,
    ignored_untracked_prefixes: tuple[str, ...] = (),
) -> bool:
    """Synchronize append-oriented mailbox commits without discarding local work."""
    run_name = threading.current_thread().name
    # Preserve the original direct helper contract when no machine-managed
    # untracked paths need to be tolerated.  The mailbox poller passes the
    # configured outbox prefix explicitly; legacy/direct callers continue to
    # use git_is_clean(), which also keeps their retry behavior unchanged.
    if ignored_untracked_prefixes:
        worktree_safe = _worktree_safe_for_mailbox_sync(
            mailbox_module, repo_path, ignored_untracked_prefixes
        )
    else:
        worktree_safe = mailbox_module.git_is_clean(repo_path)
    if not worktree_safe:
        logger.warning("Mailbox sync refused run=%s reason=dirty_worktree", run_name)
        return False

    for attempt in range(1, max_attempts + 1):
        logger.info("Mailbox sync fetch run=%s attempt=%d/%d", run_name, attempt, max_attempts)
        fetch = mailbox_module._git(["fetch"], repo_path, check=False)
        if fetch.returncode != 0:
            logger.warning(
                "Mailbox sync fetch failed run=%s attempt=%d/%d exit=%d",
                run_name,
                attempt,
                max_attempts,
                fetch.returncode,
            )
            if attempt < max_attempts:
                continue
            return False

        rebase = mailbox_module._git(["rebase", "@{u}"], repo_path, check=False)
        if rebase.returncode != 0:
            mailbox_module._git(["rebase", "--abort"], repo_path, check=False)
            logger.error(
                "Mailbox sync rebase failed run=%s attempt=%d/%d; rebase aborted and local commits were preserved",
                run_name,
                attempt,
                max_attempts,
            )
            return False

        if mailbox_module.git_push(repo_path):
            logger.info("Mailbox sync complete run=%s attempt=%d/%d", run_name, attempt, max_attempts)
            return True

        if attempt < max_attempts:
            logger.info(
                "Mailbox push rejected run=%s; retrying fetch/rebase/push (%d/%d)",
                run_name,
                attempt + 1,
                max_attempts,
            )

    logger.warning("Mailbox sync failed run=%s after=%d_attempts", run_name, max_attempts)
    return False


@dataclass
class _DrainOutcome:
    """What a drain loop produced, and whether it ended on a fault.

    Work already finished is kept in ``results`` whatever ended the loop, but
    the reason still has to reach the caller: reaching a drain bound is a
    normal, successful stop, while a resync that failed mid-drain is the same
    real fault the initial sync reports as ``sync_failed``.  Returning results
    alone would let a broken mailbox checkout exit as a clean poll.
    """

    results: list[dict[str, Any]]
    status: str = "processed"


def _drain_inbox(
    *,
    processor: Any,
    mailbox_module: Any,
    dry_run: bool,
    limits: MailboxDrainLimits,
    resync: Callable[[], bool] | None,
    run_name: str,
) -> _DrainOutcome:
    """Process the inbox, then keep re-scanning it until no new work appears.

    The inbox is the queue.  A timer invocation that finds the lock held does
    no work at all, so the processor holding the lock is the only one that can
    notice a message which arrived while it was busy - hence the rescan instead
    of a second queue or a pending-work flag.

    Only files that no earlier pass has seen are processed again.  A message
    left in the inbox on purpose - deferred, awaiting a retry, or out of
    retries - therefore waits for a later invocation rather than being re-run
    in a tight loop.

    ``resync`` refreshes the mailbox checkout between passes; new inbox
    messages reach this worker as commits, so without it a rescan could only
    ever see what was already on disk.  A failed resync ends the drain and is
    reported as ``sync_failed``: the work already done stands, but the run is
    a fault, not a clean poll.

    ``limits`` bounds the loop so continuous inbox traffic cannot keep one
    worker running forever.  Both bounds are checked between passes, never
    inside one, so the batch in flight always finishes before the worker stops.
    """
    started = time.monotonic()

    # Snapshot before processing: a message that lands between this scan and
    # the processor's own scan is then merely rescanned (and skipped as already
    # done) instead of being missed for a whole timer period.
    seen = {str(path) for path in processor.list_inbox_files()}
    results = _finalize_terminal_validation_errors(
        processor=processor,
        results=processor.process_inbox(),
        mailbox_module=mailbox_module,
        dry_run=dry_run,
    )

    passes = 1
    status = "processed"
    while True:
        if resync is not None and not resync():
            logger.warning("Mailbox drain stopped run=%s reason=sync_failed pass=%d", run_name, passes)
            status = "sync_failed"
            break

        new_files = [path for path in processor.list_inbox_files() if str(path) not in seen]
        if not new_files:
            break

        if passes >= limits.max_passes:
            logger.info(
                "Mailbox drain stopped run=%s reason=max_passes passes=%d remaining=%d",
                run_name,
                passes,
                len(new_files),
            )
            break
        elapsed = time.monotonic() - started
        if elapsed >= limits.max_seconds:
            logger.info(
                "Mailbox drain stopped run=%s reason=max_seconds elapsed=%.1f remaining=%d",
                run_name,
                elapsed,
                len(new_files),
            )
            break

        seen.update(str(path) for path in new_files)
        passes += 1
        logger.info(
            "Mailbox drain pass run=%s pass=%d new=%d", run_name, passes, len(new_files)
        )
        results.extend(
            _finalize_terminal_validation_errors(
                processor=processor,
                results=processor.process_inbox(new_files),
                mailbox_module=mailbox_module,
                dry_run=dry_run,
            )
        )

    return _DrainOutcome(results=results, status=status)


def run_mailbox_poll_once(
    *,
    db_path: str,
    mailbox_config: MailboxConfig,
    lock_path: Path,
    dry_run: bool,
    drain_limits: MailboxDrainLimits | None = None,
) -> MailboxPollRunResult:
    from modules.experiment_service import ExperimentService
    from modules import mailbox as mailbox_module
    from modules.mailbox import MailboxLock, MailboxProcessor
    from .development import install as install_mailbox_development
    from modules.operation_chain_publisher import OperationChainPublisher
    from modules.operation_chain_service import OperationChainService
    from modules.operations_registry import OperationsRegistry
    from modules.outbox_publisher import FailureAnalysisOutboxPublisher, TaskApprovalOutboxPublisher

    run_name = threading.current_thread().name
    limits = drain_limits if drain_limits is not None else MailboxDrainLimits.from_env()
    logger.info("Mailbox poll start run=%s dry_run=%s", run_name, dry_run)

    # Register unrestricted mailbox commands before any inbox file is parsed.
    # NEXUS_DEVELOPMENT_MODE only controls optional task auto-approval.
    install_mailbox_development(mailbox_module)

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = MailboxLock(lock_path)
    if not lock.acquire():
        # Normal contention, not a fault: the processor that owns the lock
        # rescans the inbox before it exits, so whatever triggered this run is
        # still picked up.  Callers translate `busy` into a successful exit.
        logger.info(
            "Mailbox poll skipped run=%s reason=busy: another processor is already running",
            run_name,
        )
        return MailboxPollRunResult(status="busy")

    result = MailboxPollRunResult(status="error", error="unknown mailbox processing failure")
    cleanup_failures: list[str] = []
    processor = None
    chain_service = None
    publisher = None
    task_publisher = None
    failure_analysis_publisher = None
    operations_registry = None
    experiment_service = None
    try:
        experiment_service = ExperimentService(db_path=db_path)
        operations_registry = OperationsRegistry(
            experiment_service=experiment_service,
            db_path=db_path,
            repo_root=str(mailbox_config.code_repo_path),
        )
        chain_service = OperationChainService(
            db_path=db_path,
            mailbox_config=mailbox_config,
            operations_registry=operations_registry,
        )
        processor = MailboxProcessor(
            repo_path=mailbox_config.mailbox_repo_path,
            db_path=db_path,
            dry_run=dry_run,
            operation_chain_service=chain_service,
            mailbox_config=mailbox_config,
            task_log_dir=getattr(experiment_service, "experiment_log_dir", DEFAULT_LOG_DIR),
        )
        publisher = OperationChainPublisher(
            db_path=db_path,
            mailbox_config=mailbox_config,
        )
        task_publisher = TaskApprovalOutboxPublisher(
            db_path=db_path,
            mailbox_config=mailbox_config,
        )
        failure_analysis_publisher = FailureAnalysisOutboxPublisher(
            db_path=db_path,
            mailbox_config=mailbox_config,
        )
        if not dry_run:
            publisher.publish_pending()
            task_publisher.publish_pending()
            failure_analysis_publisher.publish_pending()
            processor.retry_pending_git_commits()
            # Prune only after pending commits have been retried: a file
            # still needed to finalize one is protected, and retrying first
            # lets those records reach `done` so they become prunable. The
            # deletions are committed locally here and pushed by the sync
            # below, rather than opening a second push path.
            try:
                processor.prune_old_messages()
            except Exception as exc:  # noqa: BLE001 - never block the poll
                logger.warning("Mailbox prune failed run=%s type=%s", run_name, type(exc).__name__)
            try:
                outbox_prefix = (
                    mailbox_config.outbox_dir
                    .relative_to(mailbox_config.mailbox_repo_path)
                    .as_posix()
                    .rstrip("/")
                    + "/"
                )
            except ValueError:
                outbox_prefix = ""
            ignored_untracked = (outbox_prefix,) if outbox_prefix else ()
            if not _worktree_safe_for_mailbox_sync(
                mailbox_module,
                mailbox_config.mailbox_repo_path,
                ignored_untracked,
            ):
                result = MailboxPollRunResult(status="dirty_worktree")
            elif not _sync_mailbox_repo(
                mailbox_module,
                mailbox_config.mailbox_repo_path,
                ignored_untracked_prefixes=ignored_untracked,
            ):
                result = MailboxPollRunResult(status="sync_failed")
            else:
                outcome = _drain_inbox(
                    processor=processor,
                    mailbox_module=mailbox_module,
                    dry_run=False,
                    limits=limits,
                    resync=lambda: _sync_mailbox_repo(
                        mailbox_module,
                        mailbox_config.mailbox_repo_path,
                        ignored_untracked_prefixes=ignored_untracked,
                    ),
                    run_name=run_name,
                )
                logger.info(
                    "Mailbox poll processed run=%s status=%s count=%d results=%s",
                    run_name,
                    outcome.status,
                    len(outcome.results),
                    _safe_result_summary(outcome.results),
                )
                # A resync that failed mid-drain keeps the completed work but
                # still reports the fault, exactly as the initial sync does.
                result = MailboxPollRunResult(
                    status=outcome.status, results=outcome.results
                )
        else:
            # A dry run touches neither git nor the inbox, so there is nothing
            # to resync between passes; the rescan still catches a message that
            # lands while this run is working.
            outcome = _drain_inbox(
                processor=processor,
                mailbox_module=mailbox_module,
                dry_run=True,
                limits=limits,
                resync=None,
                run_name=run_name,
            )
            logger.info(
                "Mailbox poll processed run=%s count=%d results=%s",
                run_name,
                len(outcome.results),
                _safe_result_summary(outcome.results),
            )
            result = MailboxPollRunResult(
                status=outcome.status, results=outcome.results
            )
    except Exception as exc:
        logger.error("Mailbox poll exception run=%s type=%s", run_name, type(exc).__name__)
        result = MailboxPollRunResult(status="error", error=str(exc))
    finally:
        try:
            for label, resource in (
                ("processor", processor),
                ("operation_chain_service", chain_service),
                ("operation_chain_publisher", publisher),
                ("task_approval_outbox_publisher", task_publisher),
                ("failure_analysis_outbox_publisher", failure_analysis_publisher),
                ("operations_registry", operations_registry),
                ("experiment_service", experiment_service),
            ):
                if resource is None:
                    continue
                try:
                    resource.close()
                except Exception:
                    cleanup_failures.append(label)
        finally:
            try:
                lock.release()
            except Exception:
                cleanup_failures.append("mailbox_lock")

    if cleanup_failures:
        details = ", ".join(cleanup_failures)
        if result.status == "error":
            base = result.error or "unknown mailbox processing failure"
            result = MailboxPollRunResult(
                status="error",
                results=result.results,
                error=f"{base}; cleanup_failed={details}",
            )
        else:
            result = MailboxPollRunResult(
                status="error",
                results=result.results,
                error=f"mailbox processing cleanup_failed={details}",
            )

    logger.info(
        "Mailbox poll end run=%s status=%s count=%d",
        run_name,
        result.status,
        len(result.results),
    )
    return result
