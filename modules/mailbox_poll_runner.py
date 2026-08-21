from __future__ import annotations

import logging
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


def _sync_mailbox_repo(mailbox_module: Any, repo_path: Path, max_attempts: int = 3) -> bool:
    """Synchronize append-oriented mailbox commits without discarding local work."""
    if not mailbox_module.git_is_clean(repo_path):
        logger.warning("Mailbox sync refused: working tree is not clean")
        return False

    for attempt in range(1, max_attempts + 1):
        fetch = mailbox_module._git(["fetch"], repo_path, check=False)
        if fetch.returncode != 0:
            logger.warning("Mailbox sync fetch failed (exit %d)", fetch.returncode)
            return False

        rebase = mailbox_module._git(["rebase", "@{u}"], repo_path, check=False)
        if rebase.returncode != 0:
            mailbox_module._git(["rebase", "--abort"], repo_path, check=False)
            logger.error(
                "Mailbox sync rebase failed; rebase aborted and local commits were preserved"
            )
            return False

        if mailbox_module.git_push(repo_path):
            return True

        if attempt < max_attempts:
            logger.info(
                "Mailbox push rejected; retrying fetch/rebase/push (%d/%d)",
                attempt + 1,
                max_attempts,
            )

    logger.warning("Mailbox sync failed after %d attempts", max_attempts)
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

    # Register unrestricted mailbox commands before any inbox file is parsed.
    # NEXUS_DEVELOPMENT_MODE only controls optional task auto-approval.
    install_mailbox_development(mailbox_module)

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = MailboxLock(lock_path)
    if not lock.acquire():
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
                results = processor.process_inbox()
                result = MailboxPollRunResult(status="processed", results=results)
        else:
            results = processor.process_inbox()
            result = MailboxPollRunResult(status="processed", results=results)
    except Exception as exc:
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
            return MailboxPollRunResult(
                status="error",
                results=result.results,
                error=f"{base}; cleanup_failed={details}",
            )
        return MailboxPollRunResult(
            status="error",
            results=result.results,
            error=f"mailbox processing cleanup_failed={details}",
        )
    return result
