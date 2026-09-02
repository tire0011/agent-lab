from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import mailbox_processor
from modules import central_logging as central_logging_module
from modules.mailbox import MailboxLock, MailboxProcessor
from modules.redaction import bounded_lines, changed_file_names, redact, sanitized_reason, test_command_name, test_failure_tail as _test_failure_tail
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
        params = dict(args or {})
        started = time.perf_counter()
        try:
            # Executability is defined by an implemented handler, not by the
            # metadata catalog.  This avoids a second positive allowlist that can
            # silently block operations which are already implemented elsewhere.
            # Unknown names still fail safely because no handler exists.
            handler = getattr(self, f"_op_{operation}", None)
            if handler is None or not callable(handler):
                raise OperationValidationError(f"Unbekannte Operation: {operation}")
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
        limit20 = OperationArgSpec("limit", "integer", "Maximale Anzahl Prozesse.", default=20, minimum=1, maximum=200)
        experiment_id = OperationArgSpec("experiment_id", "string", "Experiment-ID oder eindeutiges Präfix.", required=True)
        adoption_id = OperationArgSpec("adoption_id", "string", "Adoption-ID.", required=True)
        service = OperationArgSpec("service", "string", "systemd-Unit-Name (Standard: agent-api).", default=_DEFAULT_SERVICE_NAME)
        return {
            "disk": OperationSpec("disk", "Datenträgerbelegung des Hosts."),
            "experiment_logs": OperationSpec("experiment_logs", "Gekürzte JSONL-Logausgabe eines Experiments.", [experiment_id, limit50]),
            "experiment_show": OperationSpec("experiment_show", "Sichere Kurzübersicht zu einem Experiment.", [experiment_id]),
            "git_log": OperationSpec("git_log", "Kurze Git-Historie des Haupt-Repositories.", [limit10]),
            "git_status": OperationSpec("git_status", "Git-Status des Haupt-Repositories."),
            "git_worktrees": OperationSpec("git_worktrees", "Liste der registrierten Git-Worktrees."),
            "help": OperationSpec("help", "Liste aller erlaubten Operationen."),
            "inbox_refresh": OperationSpec("inbox_refresh", "Genau einen kontrollierten Mailbox-Poll auslösen."),
            "memory": OperationSpec("memory", "Speicherbelegung aus /proc/meminfo."),
            "mailbox_status": OperationSpec("mailbox_status", "Status des Nexus-Mailbox-Service und -Timers."),
            "local_model_status": OperationSpec("local_model_status", "Status und Modellliste des lokalen llama.cpp-Servers."),
            "process_status": OperationSpec("process_status", "Prozessliste, sortiert nach RSS/%MEM.", [limit20]),
            "service_logs": OperationSpec("service_logs", "journalctl-Auszug für einen systemd-Service (Standard: API-Service).", [service, limit50]),
            "service_status": OperationSpec("service_status", "systemctl-Status eines systemd-Service (Standard: API-Service).", [service]),
            "adoption_show": OperationSpec("adoption_show", "Sichere Kurzübersicht zu einer Adoption.", [adoption_id]),
            "problems_list": OperationSpec("problems_list", "Liste der letzten Probleme."),
            "inbox_status": OperationSpec("inbox_status", "Status der Mailbox-Inbox."),
            "outbox_status": OperationSpec("outbox_status", "Neueste Dateien in der Mailbox-Outbox."),
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

    def _service(self, args: dict[str, Any]) -> str:
        value = args.get("service")
        if value is None:
            return self.service_name
        if not isinstance(value, str) or not value.strip():
            raise OperationValidationError("service muss ein nicht-leerer Text sein.")
        return value.strip()

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
        result.operation = "git_status"
        return result

    def _op_git_log(self, args: dict[str, Any]) -> OperationResult:
        limit = self._limit(args, default=10, maximum=100)
        result = self._run_command(["git", "log", "--oneline", "-n", str(limit)], cwd=self.repo_root, timeout=10)
        result.operation = "git_log"
        return result

    def _op_git_worktrees(self, args: dict[str, Any]) -> OperationResult:
        del args
        result = self._run_command(["git", "worktree", "list"], cwd=self.repo_root, timeout=10)
        result.operation = "git_worktrees"
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
        if experiment.test_exit_code not in (None, 0):
            tail = _test_failure_tail(experiment.test_output)
            if tail:
                lines.append("test_failure_tail:")
                lines.extend(f"  {line}" for line in tail)
        return OperationResult("experiment_show", True, lines, 0)

    def _op_experiment_logs(self, args: dict[str, Any]) -> OperationResult:
        experiment = self._experiment(args)
        limit = self._limit(args, default=50, maximum=500)
        entries = central_logging_module.read_log_entries(self.experiment_service.experiment_log_path(experiment.id), limit=limit)
        lines = [
            redact(
                f"{entry.get('timestamp', '')} {entry.get('level', '')} {entry.get('phase', '')} {entry.get('event', '')} {entry.get('message', '')}"
            )[:500]
            for entry in entries
        ]
        return OperationResult("experiment_logs", True, lines, 0)

    def _op_local_model_status(self, args: dict[str, Any]) -> OperationResult:
        if args:
            raise OperationValidationError("local_model_status akzeptiert keine Argumente.")
        started = time.perf_counter()
        base_url = (
            os.getenv("LOCAL_MODEL_URL")
            or os.getenv("LLAMA_CPP_BASE_URL")
            or "http://127.0.0.1:8080/v1"
        ).rstrip("/")
        try:
            request = urllib.request.Request(f"{base_url}/models", method="GET")
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read(4000).decode("utf-8", errors="replace")
                status = getattr(response, "status", 200)
            lines = bounded_lines(redact(body), max_lines=40)
            return OperationResult(
                operation="local_model_status",
                success=200 <= int(status) < 300,
                output_lines=[f"http_status: {status}", *lines],
                duration_ms=int((time.perf_counter() - started) * 1000),
                exit_code=0 if 200 <= int(status) < 300 else int(status),
                error_summary=None if 200 <= int(status) < 300 else f"local model HTTP {status}",
            )
        except urllib.error.HTTPError as exc:
            return OperationResult(
                operation="local_model_status",
                success=False,
                output_lines=[],
                duration_ms=int((time.perf_counter() - started) * 1000),
                exit_code=exc.code,
                error_summary=f"local model HTTP {exc.code}",
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return OperationResult(
                operation="local_model_status",
                success=False,
                output_lines=[],
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_summary=sanitized_reason(str(exc)),
            )

    def _op_process_status(self, args: dict[str, Any]) -> OperationResult:
        limit = self._limit(args, default=20, maximum=200)
        result = self._run_command(["ps", "-eo", "pid,ppid,pmem,rss,comm", "--sort=-rss"], timeout=15)
        result.operation = "process_status"
        if result.success and result.output_lines:
            header, *rows = result.output_lines
            result.output_lines = [header] + rows[:limit]
        return result

    def _op_service_status(self, args: dict[str, Any]) -> OperationResult:
        service = self._service(args)
        result = self._run_command(["systemctl", "status", service, "--no-pager", "-n", "0"], timeout=15)
        result.operation = "service_status"
        return result

    def _op_mailbox_status(self, args: dict[str, Any]) -> OperationResult:
        if args:
            raise OperationValidationError("mailbox_status akzeptiert keine Argumente.")

        started = time.perf_counter()
        checks = []
        for unit in ("nexus-mailbox.service", "nexus-mailbox.timer"):
            check = self._run_command(
                ["systemctl", "status", unit, "--no-pager", "-n", "0"],
                timeout=15,
            )
            checks.append((unit, check))

        def check_is_healthy(unit: str, check: OperationResult) -> bool:
            if check.success:
                return True
            if unit != "nexus-mailbox.service" or check.exit_code != 3:
                return False
            # systemctl status returns 3 for an inactive unit. That is the
            # expected steady state for this timer-triggered oneshot service
            # after a successful run, so treat it as healthy only when the
            # status output proves both facts explicitly.
            status_text = "\n".join(check.output_lines).lower()
            return (
                "active: inactive (dead)" in status_text
                and "status=0/success" in status_text
            )

        output_lines: list[str] = []
        failure_details: list[str] = []
        failed_checks: list[OperationResult] = []
        for unit, check in checks:
            output_lines.append(f"{unit}:")
            # Reserve room for both unit labels and exit-code lines while
            # keeping the combined response within the 200-line bound.
            output_lines.extend(bounded_lines("\n".join(check.output_lines), max_lines=96))
            output_lines.append(f"exit_code: {check.exit_code if check.exit_code is not None else '-'}")
            if not check_is_healthy(unit, check):
                failed_checks.append(check)
                reason = sanitized_reason(check.error_summary) or "Statusprüfung fehlgeschlagen."
                failure_details.append(f"{unit}: {reason}")

        return OperationResult(
            operation="mailbox_status",
            success=not failed_checks,
            output_lines=bounded_lines("\n".join(output_lines), max_lines=200),
            duration_ms=int((time.perf_counter() - started) * 1000),
            exit_code=failed_checks[0].exit_code if failed_checks else 0,
            error_summary=sanitized_reason("; ".join(failure_details)) if failure_details else None,
        )

    def _op_service_logs(self, args: dict[str, Any]) -> OperationResult:
        service = self._service(args)
        limit = self._limit(args, default=50, maximum=500)
        result = self._run_command(["journalctl", "-u", service, "-n", str(limit), "--no-pager"], timeout=20)
        result.operation = "service_logs"
        return result

    def _op_inbox_refresh(self, args: dict[str, Any]) -> OperationResult:
        del args
        self.mailbox_lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = MailboxLock(self.mailbox_lock_path)
        if not lock.acquire():
            return OperationResult("inbox_refresh", False, ["Mailbox-Poll läuft bereits."], 0, error_summary="Mailbox-Poll läuft bereits.")
        started = time.perf_counter()
        try:
            processor = self.mailbox_processor_factory(repo_path=self.repo_root, db_path=self.db_path, dry_run=False)
            try:
                processor.retry_unpushed_commits()
                processor.retry_pending_git_commits()
                if not processor.ensure_clean_worktree():
                    return OperationResult(
                        "inbox_refresh",
                        False,
                        ["Working tree is dirty; refusing mailbox processing"],
                        int((time.perf_counter() - started) * 1000),
                        error_summary="Working tree is dirty; refusing mailbox processing",
                    )
                if not processor.pull():
                    return OperationResult(
                        "inbox_refresh",
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
        return OperationResult("inbox_refresh", True, output, int((time.perf_counter() - started) * 1000))

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

    def _op_problems_list(self, args: dict[str, Any]) -> OperationResult:
        del args
        from data_sources import list_problems
        problems = list_problems()
        lines = [f"{p.id}: {p.summary}" for p in problems[:20]]
        return OperationResult("problems_list", True, lines, 0)

    def _op_adoption_show(self, args: dict[str, Any]) -> OperationResult:
        adoption_id = args.get("adoption_id")
        if not isinstance(adoption_id, str) or not adoption_id.strip():
            raise OperationValidationError("adoption_id ist erforderlich.")
        if not self.experiment_service:
            raise OperationValidationError("experiment_service nicht initialisiert.")
        try:
            adoption = self.experiment_service.get_adoption(adoption_id.strip())
            lines = [
                f"id: {adoption.id}",
                f"experiment_id: {adoption.experiment_id}",
                f"status: {adoption.status}",
                f"started_at: {adoption.started_at or '-'}",
                f"ended_at: {adoption.ended_at or '-'}",
                f"target_branch: {adoption.target_branch}",
                f"test_exit_code: {adoption.test_exit_code if adoption.test_exit_code is not None else '-'}",
            ]
            return OperationResult("adoption_show", True, lines, 0)
        except Exception as exc:
            raise OperationValidationError(f"adoption not found: {exc}")

    def _op_inbox_status(self, args: dict[str, Any]) -> OperationResult:
        del args
        conn = init_db(self.db_path)
        try:
            pending = conn.execute(
                "SELECT COUNT(*) FROM mailbox_messages WHERE status NOT IN ('done', 'done_pending_commit')"
            ).fetchone()[0]
            return OperationResult("inbox_status", True, [f"pending_count: {pending}"], 0)
        finally:
            conn.close()

    def _op_outbox_status(self, args: dict[str, Any]) -> OperationResult:
        del args
        from pathlib import Path
        outbox_dir = Path("/agent/communication/outbox")
        if not outbox_dir.exists():
            return OperationResult("outbox_status", True, ["outbox_dir does not exist"], 0)
        files = sorted(outbox_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
        lines = [f"{f.name}" for f in files]
        return OperationResult("outbox_status", True, lines, 0)
