from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import mailbox_processor
from modules import experiment_logger as experiment_logger_module
from modules.mailbox import MailboxLock, MailboxProcessor
from modules.redaction import bounded_lines, changed_file_names, redact, sanitized_reason, test_command_name
from modules.run_store import DEFAULT_DB_PATH, init_db

logger = logging.getLogger(__name__)

_DEFAULT_REPO_ROOT = "/agent"
_DEFAULT_MAILBOX_LOCK_PATH = "/var/lib/nexus/mailbox.lock"
_DEFAULT_SERVICE_NAME = "agent-api"


class OperationValidationError(ValueError):
    pass


@dataclass(slots=True)
class OperationArgSpec:
    name: str
    type: str
    description: str
    required: bool = False
    default: int | str | None = None
    minimum: int | None = None
    maximum: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "required": self.required,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


@dataclass(slots=True)
class OperationSpec:
    name: str
    description: str
    arguments: list[OperationArgSpec] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.name,
            "description": self.description,
            "arguments": [argument.to_dict() for argument in self.arguments],
        }


@dataclass(slots=True)
class OperationResult:
    operation: str
    success: bool
    output_lines: list[str]
    duration_ms: int
    exit_code: int | None = None
    error_summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "success": self.success,
            "output_lines": list(self.output_lines),
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "error_summary": self.error_summary,
        }


class OperationsRegistry:
    def __init__(
        self,
        *,
        experiment_service,
        db_path: str = DEFAULT_DB_PATH,
        repo_root: str = _DEFAULT_REPO_ROOT,
        mailbox_lock_path: str = _DEFAULT_MAILBOX_LOCK_PATH,
        service_name: str = _DEFAULT_SERVICE_NAME,
        mailbox_processor_factory: Callable[..., MailboxProcessor] = MailboxProcessor,
        subprocess_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        meminfo_path: str = "/proc/meminfo",
    ) -> None:
        self.experiment_service = experiment_service
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self.mailbox_lock_path = Path(mailbox_lock_path)
        self.service_name = service_name
        self.mailbox_processor_factory = mailbox_processor_factory
        self.subprocess_runner = subprocess_runner
        self.meminfo_path = Path(meminfo_path)
        conn = init_db(db_path)
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._specs = self._build_specs()

    def close(self) -> None:
        self._conn.close()

    def catalog(self) -> list[OperationSpec]:
        return [self._specs[name] for name in sorted(self._specs)]

    def execute(self, operation: str, args: dict[str, Any] | None = None, *, caller: str = "authenticated caller") -> OperationResult:
        if operation not in self._specs:
            raise OperationValidationError("Operation ist nicht erlaubt.")
        params = dict(args or {})
        started = time.perf_counter()
        try:
            handler = getattr(self, f"_op_{operation.replace(' ', '_')}")
            result = handler(params)
        except OperationValidationError:
            raise
        except Exception as exc:  # noqa: BLE001
            result = OperationResult(
                operation=operation,
                success=False,
                output_lines=[],
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_summary=sanitized_reason(str(exc)),
            )
        self._audit(operation, params, result, caller=caller)
        return result

    def _build_specs(self) -> dict[str, OperationSpec]:
        limit10 = OperationArgSpec("limit", "integer", "Maximale Anzahl Zeilen/Einträge.", default=10, minimum=1, maximum=100)
        limit50 = OperationArgSpec("limit", "integer", "Maximale Anzahl Zeilen/Einträge.", default=50, minimum=1, maximum=500)
        experiment_id = OperationArgSpec("experiment_id", "string", "Experiment-ID oder eindeutiges Präfix.", required=True)
        return {
            "disk": OperationSpec("disk", "Datenträgerbelegung des Hosts."),
            "experiment logs": OperationSpec("experiment logs", "Gekürzte JSONL-Logausgabe eines Experiments.", [experiment_id, limit50]),
            "experiment show": OperationSpec("experiment show", "Sichere Kurzübersicht zu einem Experiment.", [experiment_id]),
            "git log": OperationSpec("git log", "Kurze Git-Historie des Haupt-Repositories.", [limit10]),
            "git status": OperationSpec("git status", "Git-Status des Haupt-Repositories."),
            "git worktrees": OperationSpec("git worktrees", "Liste der registrierten Git-Worktrees."),
            "help": OperationSpec("help", "Liste aller erlaubten Operationen."),
            "inbox refresh": OperationSpec("inbox refresh", "Genau einen kontrollierten Mailbox-Poll auslösen."),
            "memory": OperationSpec("memory", "Speicherbelegung aus /proc/meminfo."),
            "service logs": OperationSpec("service logs", "journalctl-Auszug für den API-Service.", [limit50]),
            "service status": OperationSpec("service status", "systemctl-Status des API-Service."),
        }

    def _audit(self, operation: str, args: dict[str, Any], result: OperationResult, *, caller: str) -> None:
        del caller  # never persist credentials or identifiers from the API key
        self._conn.execute(
            """
            INSERT INTO operations_audit (id, timestamp, operation, args_json, success, duration_ms, error_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                operation,
                json.dumps({k: redact(str(v)) for k, v in args.items()}, sort_keys=True),
                1 if result.success else 0,
                result.duration_ms,
                sanitized_reason(result.error_summary),
            ),
        )
        self._conn.commit()

    def _limit(self, args: dict[str, Any], *, default: int, maximum: int) -> int:
        value = default if args.get("limit") is None else args.get("limit")
        if isinstance(value, bool) or not isinstance(value, int):
            raise OperationValidationError("limit muss eine ganze Zahl sein.")
        if value < 1 or value > maximum:
            raise OperationValidationError(f"limit muss zwischen 1 und {maximum} liegen.")
        return value

    def _experiment(self, args: dict[str, Any]):
        experiment_id = args.get("experiment_id")
        if not isinstance(experiment_id, str) or not experiment_id.strip():
            raise OperationValidationError("experiment_id ist erforderlich.")
        return self.experiment_service.resolve_experiment(experiment_id.strip())

    def _run_command(self, command: list[str], *, cwd: Path | None = None, timeout: int = 15) -> OperationResult:
        started = time.perf_counter()
        try:
            completed = self.subprocess_runner(
                command,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
            output = bounded_lines((completed.stdout or "") + (completed.stderr or ""), max_lines=200)
            return OperationResult(
                operation=" ".join(command[:2]),
                success=completed.returncode == 0,
                output_lines=output,
                duration_ms=int((time.perf_counter() - started) * 1000),
                exit_code=completed.returncode,
                error_summary=None if completed.returncode == 0 else sanitized_reason((completed.stderr or completed.stdout or "command failed")),
            )
        except subprocess.TimeoutExpired:
            return OperationResult(
                operation=" ".join(command[:2]),
                success=False,
                output_lines=[],
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_summary="Operation hat das Zeitlimit überschritten.",
            )
        except OSError as exc:
            return OperationResult(
                operation=" ".join(command[:2]),
                success=False,
                output_lines=[],
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_summary=sanitized_reason(str(exc)),
            )

    def _op_help(self, args: dict[str, Any]) -> OperationResult:
        del args
        lines = [f"{spec.name}: {spec.description}" for spec in self.catalog()]
        return OperationResult("help", True, lines, 0)

    def _op_git_status(self, args: dict[str, Any]) -> OperationResult:
        del args
        result = self._run_command(["git", "status", "--porcelain"], cwd=self.repo_root, timeout=10)
        result.operation = "git status"
        return result

    def _op_git_log(self, args: dict[str, Any]) -> OperationResult:
        limit = self._limit(args, default=10, maximum=100)
        result = self._run_command(["git", "log", "--oneline", "-n", str(limit)], cwd=self.repo_root, timeout=10)
        result.operation = "git log"
        return result

    def _op_git_worktrees(self, args: dict[str, Any]) -> OperationResult:
        del args
        result = self._run_command(["git", "worktree", "list"], cwd=self.repo_root, timeout=10)
        result.operation = "git worktrees"
        return result

    def _op_experiment_show(self, args: dict[str, Any]) -> OperationResult:
        experiment = self._experiment(args)
        lines = [
            f"id: {experiment.id}",
            f"status: {experiment.status}",
            f"created_at: {experiment.created_at}",
            f"ended_at: {experiment.ended_at or '-'}",
            f"test_command: {test_command_name(experiment.test_command) or '-'}",
            f"test_exit_code: {experiment.test_exit_code if experiment.test_exit_code is not None else '-'}",
            f"changed_files: {', '.join(changed_file_names(experiment.git_status)) or '-'}",
            f"reason: {sanitized_reason(experiment.error) or '-'}",
        ]
        return OperationResult("experiment show", True, lines, 0)

    def _op_experiment_logs(self, args: dict[str, Any]) -> OperationResult:
        experiment = self._experiment(args)
        limit = self._limit(args, default=50, maximum=500)
        entries = experiment_logger_module.read_log_entries(self.experiment_service.experiment_log_path(experiment.id), limit=limit)
        lines = [
            redact(
                f"{entry.get('timestamp', '')} {entry.get('level', '')} {entry.get('phase', '')} {entry.get('event', '')} {entry.get('message', '')}"
            )[:500]
            for entry in entries
        ]
        return OperationResult("experiment logs", True, lines, 0)

    def _op_service_status(self, args: dict[str, Any]) -> OperationResult:
        del args
        result = self._run_command(["systemctl", "status", self.service_name, "--no-pager", "-n", "0"], timeout=15)
        result.operation = "service status"
        return result

    def _op_service_logs(self, args: dict[str, Any]) -> OperationResult:
        limit = self._limit(args, default=50, maximum=500)
        result = self._run_command(["journalctl", "-u", self.service_name, "-n", str(limit), "--no-pager"], timeout=20)
        result.operation = "service logs"
        return result

    def _op_inbox_refresh(self, args: dict[str, Any]) -> OperationResult:
        del args
        self.mailbox_lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = MailboxLock(self.mailbox_lock_path)
        if not lock.acquire():
            return OperationResult("inbox refresh", False, ["Mailbox-Poll läuft bereits."], 0, error_summary="Mailbox-Poll läuft bereits.")
        started = time.perf_counter()
        try:
            processor = self.mailbox_processor_factory(repo_path=self.repo_root, db_path=self.db_path, dry_run=False)
            try:
                processor.retry_unpushed_commits()
                processor.retry_pending_git_commits()
                if not processor.ensure_clean_worktree():
                    return OperationResult(
                        "inbox refresh",
                        False,
                        ["Working tree is dirty; refusing mailbox processing"],
                        int((time.perf_counter() - started) * 1000),
                        error_summary="Working tree is dirty; refusing mailbox processing",
                    )
                if not processor.pull():
                    return OperationResult(
                        "inbox refresh",
                        False,
                        ["git pull --ff-only failed; refusing mailbox processing"],
                        int((time.perf_counter() - started) * 1000),
                        error_summary="git pull --ff-only failed; refusing mailbox processing",
                    )
                results = processor.process_inbox()
            finally:
                processor.close()
        finally:
            lock.release()
        output = [redact(json.dumps(result, sort_keys=True))[:500] for result in results] or ["Keine Inbox-Nachrichten verarbeitet."]
        return OperationResult("inbox refresh", True, output, int((time.perf_counter() - started) * 1000))

    def _op_disk(self, args: dict[str, Any]) -> OperationResult:
        del args
        usage = shutil.disk_usage("/")
        lines = [
            f"total_bytes: {usage.total}",
            f"used_bytes: {usage.used}",
            f"free_bytes: {usage.free}",
        ]
        return OperationResult("disk", True, lines, 0)

    def _op_memory(self, args: dict[str, Any]) -> OperationResult:
        del args
        if not self.meminfo_path.is_file():
            return OperationResult("memory", False, [], 0, error_summary="/proc/meminfo ist nicht verfügbar.")
        interesting = {"MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree"}
        lines = []
        for raw_line in self.meminfo_path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = raw_line.partition(":")
            if key in interesting:
                lines.append(f"{key}: {value.strip()}")
        return OperationResult("memory", True, lines, 0)
