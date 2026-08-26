from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modules.mailbox_config import MailboxConfig


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
        safe_error = mailbox_module._redact(error)[:500]
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


def _sync_mailbox_repo(mailbox_module: Any, repo_path: Path, max_attempts: int = 3) -> bool:
    """Synchronize append-oriented mailbox commits without discarding local work."""
    run_name = threading.current_thread().name
    if not mailbox_module.git_is_clean(repo_path):
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


def run_mailbox_poll_once(
    *,
    db_path: str,
    mailbox_config: MailboxConfig,
    lock_path: Path,
    dry_run: bool,
) -> MailboxPollRunResult:
    from modules.experiment_service import ExperimentService
    from modules import mailbox as mailbox_module
    from modules.mailbox import MailboxLock, MailboxProcessor
    from modules.mailbox_development import install as install_mailbox_development
    from modules.operation_chain_publisher import OperationChainPublisher
    from modules.operation_chain_service import OperationChainService
    from modules.operations_registry import OperationsRegistry
    from modules.outbox_publisher import TaskApprovalOutboxPublisher

    run_name = threading.current_thread().name
    logger.info("Mailbox poll start run=%s dry_run=%s", run_name, dry_run)

    # Register unrestricted mailbox commands before any inbox file is parsed.
    # NEXUS_DEVELOPMENT_MODE only controls optional task auto-approval.
    install_mailbox_development(mailbox_module)

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = MailboxLock(lock_path)
    if not lock.acquire():
        logger.warning("Mailbox poll skipped run=%s reason=busy", run_name)
        return MailboxPollRunResult(status="busy")

    result = MailboxPollRunResult(status="error", error="unknown mailbox processing failure")
    cleanup_failures: list[str] = []
    processor = None
    chain_service = None
    publisher = None
    task_publisher = None
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
        )
        publisher = OperationChainPublisher(
            db_path=db_path,
            mailbox_config=mailbox_config,
        )
        task_publisher = TaskApprovalOutboxPublisher(
            db_path=db_path,
            mailbox_config=mailbox_config,
        )
        if not dry_run:
            publisher.publish_pending()
            task_publisher.publish_pending()
            processor.retry_pending_git_commits()
            if not processor.ensure_clean_worktree():
                result = MailboxPollRunResult(status="dirty_worktree")
            elif not _sync_mailbox_repo(
                mailbox_module,
                mailbox_config.mailbox_repo_path,
            ):
                result = MailboxPollRunResult(status="sync_failed")
            else:
                results = _finalize_terminal_validation_errors(
                    processor=processor,
                    results=processor.process_inbox(),
                    mailbox_module=mailbox_module,
                    dry_run=False,
                )
                logger.info(
                    "Mailbox poll processed run=%s count=%d results=%s",
                    run_name,
                    len(results),
                    _safe_result_summary(results),
                )
                result = MailboxPollRunResult(status="processed", results=results)
        else:
            results = _finalize_terminal_validation_errors(
                processor=processor,
                results=processor.process_inbox(),
                mailbox_module=mailbox_module,
                dry_run=True,
            )
            logger.info(
                "Mailbox poll processed run=%s count=%d results=%s",
                run_name,
                len(results),
                _safe_result_summary(results),
            )
            result = MailboxPollRunResult(status="processed", results=results)
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
