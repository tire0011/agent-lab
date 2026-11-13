from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from modules import central_logging as central_logging_module
from modules.mailbox import MailboxLock, MailboxProcessor
from modules.redaction import bounded_lines, changed_file_names, redact, sanitized_reason, test_command_name, test_failure_tail as _test_failure_tail
from modules.run_store import DEFAULT_DB_PATH, init_db

logger = logging.getLogger(__name__)

_DEFAULT_REPO_ROOT = "/agent"
_DEFAULT_MAILBOX_LOCK_PATH = "/var/lib/nexus/mailbox.lock"
_DEFAULT_SERVICE_NAME = "agent-api"
# The services the controller may restart (#315); never the API itself.
_RESTARTABLE_SERVICES = ("nexus-mailbox", "llama-server")
_LOCAL_MODEL_RESTART_TIMEOUT_SECONDS = 120.0
_LOCAL_MODEL_RESTART_POLL_SECONDS = 2.0
# machine_reboot (#319): handed to a transient systemd unit so the reply can
# still be written before the machine goes down. The sudoers rule from
# scripts/setup-nexus-permissions.sh allows exactly this command.
INSTALL_TOOL_COMMAND = ("sudo", "-n", "/usr/local/sbin/nexus-install-tool")
_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]{1,62}$")
MACHINE_REBOOT_COMMAND = ("sudo", "-n", "systemd-run", "--collect", "--on-active=60", "systemctl", "reboot")
# Experiment statuses that a reboot would interrupt. Queued work has not
# started and remains durable across a reboot, so it must not prevent one.
_ACTIVE_EXPERIMENT_STATUSES = ("selecting", "implementing", "verifying")
_GITHUB_MCP_MAX_LINES = 400


def _default_github_work_tracking_factory(*, db_path: str, repo_root: str) -> Any:
    from modules.github_work_tracking import GitHubWorkTrackingService

    return GitHubWorkTrackingService(db_path=db_path, repo_root=repo_root)


def _default_github_mcp_config() -> Any:
    from modules.github_mcp import GitHubMcpConfig

    return GitHubMcpConfig.from_env()


# Re-exported, not redefined: these live in a leaf module so
# ``nexus_tool_router`` can import them without completing this module first.
# See modules/operation_contract.py.
from modules.operation_contract import (  # noqa: E402
    OperationArgSpec,
    OperationResult,
    OperationSpec,
    OperationValidationError,
    normalize_integer_argument,
)

__all__ = [
    "OperationArgSpec",
    "OperationResult",
    "OperationSpec",
    "OperationValidationError",
    "OperationsRegistry",
    "normalize_integer_argument",
]


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
        github_work_tracking_factory: Callable[..., Any] | None = None,
        github_mcp_config_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.experiment_service = experiment_service
        self.db_path = db_path
        self.repo_root = Path(repo_root)
        self.mailbox_lock_path = Path(mailbox_lock_path)
        self.service_name = service_name
        self.mailbox_processor_factory = mailbox_processor_factory
        self.subprocess_runner = subprocess_runner
        self.meminfo_path = Path(meminfo_path)
        # Imported lazily by default: github_status is one operation among
        # many, and the tracking service opens its own database connection.
        self.github_work_tracking_factory = (
            github_work_tracking_factory or _default_github_work_tracking_factory
        )
        # #41: read from the environment per call, so a changed token or
        # server path takes effect without a restart.
        self.github_mcp_config_factory = github_mcp_config_factory or _default_github_mcp_config
        conn = init_db(db_path)
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._specs = self._build_specs()

    def close(self) -> None:
        self._conn.close()

    def catalog(self) -> list[OperationSpec]:
        return [self._specs[name] for name in sorted(self._specs)]

    def _normalize_args(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        spec = self._specs.get(operation)
        if spec is None:
            return args

        normalized = dict(args)
        for argument in spec.arguments:
            if argument.name not in normalized or argument.type != "integer":
                continue
            normalized[argument.name] = normalize_integer_argument(
                argument,
                normalized[argument.name],
            )
        return normalized

    def execute(
        self,
        operation: str,
        args: dict[str, Any] | None = None,
        *,
        caller: str = "authenticated caller",
        invocation_id: str | None = None,
    ) -> OperationResult:
        params = self._normalize_args(operation, dict(args or {}))
        invocation_id = self._normalize_invocation_id(invocation_id)
        if invocation_id is not None:
            recorded = self._recorded_result(invocation_id, operation, params)
            if recorded is not None:
                return recorded
        started = time.perf_counter()
        try:
            # Executable means "has a handler", not "is in the catalog": no
            # second allowlist; unknown names still fail because no handler exists.
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
        self._audit(
            operation,
            params,
            result,
            caller=caller,
            invocation_id=invocation_id,
        )
        return result

    @staticmethod
    def _normalize_invocation_id(invocation_id: str | None) -> str | None:
        if invocation_id is None:
            return None
        if not isinstance(invocation_id, str):
            raise OperationValidationError("invocation_id muss eine Zeichenfolge sein.")
        value = invocation_id.strip()
        if not value or len(value) > 200:
            raise OperationValidationError("invocation_id muss 1 bis 200 Zeichen lang sein.")
        return value

    @staticmethod
    def _logged_params(args: dict[str, Any]) -> dict[str, str]:
        return {k: redact(str(v)) for k, v in args.items()}

    def _recorded_result(
        self,
        invocation_id: str,
        operation: str,
        args: dict[str, Any],
    ) -> OperationResult | None:
        row = self._conn.execute(
            "SELECT operation, parameters, result FROM operation_log WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        if row is None:
            return None
        expected_params = json.dumps(self._logged_params(args), sort_keys=True)
        if row["operation"] != operation or row["parameters"] != expected_params:
            raise OperationValidationError(
                "invocation_id wurde bereits für eine andere Operation oder andere Argumente verwendet."
            )
        payload = json.loads(row["result"])
        return OperationResult(
            operation=payload["operation"],
            success=bool(payload["success"]),
            output_lines=list(payload.get("output_lines") or []),
            duration_ms=int(payload.get("duration_ms") or 0),
            exit_code=payload.get("exit_code"),
            error_summary=payload.get("error_summary"),
            metrics=dict(payload.get("metrics") or {}),
        )

    def _build_specs(self) -> dict[str, OperationSpec]:
        limit10 = OperationArgSpec("limit", "integer", "Maximale Anzahl Zeilen/Einträge.", default=10, minimum=1, maximum=100)
        limit50 = OperationArgSpec("limit", "integer", "Maximale Anzahl Zeilen/Einträge.", default=50, minimum=1, maximum=500)
        limit20 = OperationArgSpec("limit", "integer", "Maximale Anzahl Prozesse.", default=20, minimum=1, maximum=200)
        since_timestamp = OperationArgSpec("since", "string", "Nur Probleme seit diesem ISO-Zeitstempel.")
        experiment_id = OperationArgSpec("experiment_id", "string", "Experiment-ID oder eindeutiges Präfix.", required=True)
        adoption_id = OperationArgSpec("adoption_id", "string", "Adoption-ID.", required=True)
        service = OperationArgSpec("service", "string", "systemd-Unit-Name oder Kurzname (api, mailbox, llm; Standard: agent-api).", default=_DEFAULT_SERVICE_NAME)
        since_minutes = OperationArgSpec("since_minutes", "integer", "Nur Einträge aus den letzten N Minuten.", minimum=1, maximum=60 * 24 * 7)
        reference = OperationArgSpec(
            "reference",
            "string",
            "Mailbox-Task-ID, Experiment-ID, Session-ID, Issue-/PR-Nummer (188, #188, PR #195), "
            "Inbox-Dateiname oder eindeutiges ID-Präfix.",
            required=True,
        )
        issue_number = OperationArgSpec(
            "issue_number",
            "integer",
            "GitHub-Issue-Nummer.",
            required=True,
            minimum=1,
        )
        diagnostic_tags = [
            "diagnose", "diagnostics", "logs", "log", "status", "lifecycle", "state",
            "task", "experiment", "session", "issue", "pull request", "branch",
            "stalled", "stuck", "interrupted", "heartbeat", "progress", "why",
        ]
        return {
            "disk": OperationSpec("disk", "Datenträgerbelegung des Hosts.", tags=["disk", "storage", "filesystem", "usage"], mutates_state=False),
            "experiment_logs": OperationSpec("experiment_logs", "Gekürzte JSONL-Logausgabe eines Experiments.", [experiment_id, limit50], ["experiment", "logs", "jsonl", "history"], mutates_state=False),
            "experiment_show": OperationSpec("experiment_show", "Sichere Kurzübersicht zu einem Experiment.", [experiment_id], ["experiment", "status", "summary", "inspect"], mutates_state=False),
            "branch_drift": OperationSpec("branch_drift", "Branch-Abstand und überlappende Dateien eines Experiments zu aktuellem main.", [experiment_id], ["experiment", "branch", "drift", "conflict", "overlap"], mutates_state=False),
            "experiment_reflection": OperationSpec("experiment_reflection", "Gespeicherte Reflexion (Verbesserungsvorschläge) zu einem Experiment.", [experiment_id], ["experiment", "reflection", "suggestions", "lessons", "ideas"], mutates_state=False),
            "reflections_list": OperationSpec("reflections_list", "Neueste gespeicherte Experiment-Reflexionen mit ihren Vorschlägen.", [limit10], ["reflection", "suggestions", "lessons", "ideas", "history"], mutates_state=False),
            "local_replays_list": OperationSpec("local_replays_list", "Lokale Shadow-Replays gemergter Experimente mit lokaler Test-Passrate je Prompt-Variante.", [limit10], ["local", "replay", "benchmark", "pass rate", "variant", "local model"], mutates_state=False),
            "git_log": OperationSpec("git_log", "Kurze Git-Historie des Haupt-Repositories.", [limit10], ["git", "commits", "history", "repository"], mutates_state=False),
            "git_status": OperationSpec("git_status", "Git-Status des Haupt-Repositories.", tags=["git", "status", "repository", "changes", "local changes", "working tree"], mutates_state=False),
            "git_worktrees": OperationSpec("git_worktrees", "Liste der registrierten Git-Worktrees.", tags=["git", "worktree", "repository", "checkout"], mutates_state=False),
            "help": OperationSpec("help", "Liste aller erlaubten Operationen.", tags=["help", "catalog", "operations"], mutates_state=False),
            "inbox_refresh": OperationSpec("inbox_refresh", "Genau einen kontrollierten Mailbox-Poll auslösen.", tags=["mailbox", "poll", "refresh", "write"], mutates_state=True),
            "memory": OperationSpec("memory", "Speicherbelegung aus /proc/meminfo.", tags=["memory", "ram", "host", "usage"], mutates_state=False),
            "mailbox_status": OperationSpec("mailbox_status", "Status des Nexus-Mailbox-Service und -Timers.", tags=["mailbox", "status", "timer", "service"], mutates_state=False),
            "local_model_status": OperationSpec("local_model_status", "Status und Modellliste des lokalen llama.cpp-Servers.", tags=["local model", "llama", "llm", "server", "models"], mutates_state=False),
            "local_model_schema_check": OperationSpec(
                "local_model_schema_check",
                "Prüft, ob der lokale Modellserver die JSON-Schemas von Nexus "
                "tatsächlich erzwingt. Ein ignoriertes Schema ist unsichtbar - es "
                "sieht aus wie ein Modell, das zufällig richtig antwortet - und "
                "führt dazu, dass eine korrekte Tool-Auswahl verworfen wird (#218). "
                "Kostet zwei echte Modellaufrufe und dauert daher Minuten.",
                tags=[
                    "local model", "llama", "llm", "schema", "json schema",
                    "response format", "grammar", "constraint", "enforcement",
                    "tool resolver", "diagnostics", "self check",
                ],
                mutates_state=False,
            ),
            "process_status": OperationSpec("process_status", "Prozessliste, sortiert nach RSS/%MEM.", [limit20], ["process", "memory", "rss", "host"], mutates_state=False),
            "vm_maintenance_report": OperationSpec(
                "vm_maintenance_report",
                "Kurzer, rein lesender VM-Wartungsbericht: Updates, fehlgeschlagene "
                "systemd-Units und Timer, Ressourcen, Prozesse und Zertifikate.",
                tags=[
                    "vm", "maintenance", "updates", "security", "systemd", "timer",
                    "disk", "swap", "memory pressure", "process", "certificate", "expiry",
                ],
                mutates_state=False,
            ),
            "service_logs": OperationSpec("service_logs", "journalctl-Auszug für einen systemd-Service (Standard: API-Service).", [service, limit50, since_minutes], ["service", "logs", "journalctl", "systemd"], mutates_state=False),
            "nexus_logs": OperationSpec(
                "nexus_logs",
                "Lifecycle-Status und begrenzte, korrelierte Logs zu einem Task, "
                "Experiment, einer Session oder Issue-/PR-Nummer - in einem Aufruf, "
                "ohne Log-Pfade oder journalctl-Kommandos (#188).",
                [reference, limit50],
                diagnostic_tags,
                mutates_state=False,
            ),
            "nexus_status": OperationSpec(
                "nexus_status",
                "Nur der Lifecycle-Status (queued/running/failed/merged ...) inklusive "
                "Branch, Commit, PR, Worker, letztem Fortschritt und letztem Fehler (#188).",
                [reference],
                diagnostic_tags,
                mutates_state=False,
            ),
            "nexus_last_failure": OperationSpec(
                "nexus_last_failure",
                "Die zuletzt fehlgeschlagene oder stehengebliebene Arbeit samt Fehlerkontext. "
                "Stehengeblieben zählt mit: ein mitten im Lauf gestorbener Prozess "
                "schreibt nie einen failed-Status (#188).",
                [limit50],
                [*diagnostic_tags, "failure", "last failure", "error", "crash"],
                mutates_state=False,
            ),
            "nexus_llm_job": OperationSpec(
                "nexus_llm_job",
                "Ein gespeicherter lokaler LLM-Job read-only: Rolle, Session, Status, "
                "Zeitstempel, Dauer, Prompt-Größe, Ergebnis und Fehler; mit view=result "
                "das volle Ergebnis, mit view=prompt System-Prompt und Prompt. "
                "Secrets bleiben maskiert (#259).",
                [
                    OperationArgSpec("job_id", "string", "Exakte ID des lokalen LLM-Jobs.", required=True),
                    OperationArgSpec("view", "string", "summary (Standard), result oder prompt.", default="summary"),
                ],
                [
                    *diagnostic_tags, "local model", "llm", "llm job", "job", "controller",
                    "prompt", "result", "debug",
                ],
                mutates_state=False,
            ),
            "service_restart": OperationSpec(
                "service_restart",
                "Einen Nexus-Service neu starten: mailbox (nexus-mailbox) oder llm (llama-server); "
                "bei llm bis zur erneuten Bereitschaft warten.",
                [OperationArgSpec("service", "string", "mailbox oder llm.", required=True)],
                ["service", "restart", "systemd", "mailbox", "llm", "llama"],
                mutates_state=True,
                controller_allowed=True,
            ),
            "machine_reboot": OperationSpec(
                "machine_reboot",
                "Die VM in 60 Sekunden neu starten, z. B. nach einem Ubuntu-Update. "
                "Wird verweigert, solange ein Experiment aktiv ist.",
                tags=["machine", "vm", "host", "reboot", "restart", "update", "ubuntu", "system"],
                mutates_state=True,
                controller_allowed=True,
            ),
            "install_tool": OperationSpec(
                "install_tool",
                "Ein fehlendes Werkzeug als Ubuntu-Paket installieren (apt, z. B. ripgrep für rg). "
                "Dienste, Container und Rechte-Pakete stehen auf einer Sperrliste.",
                [OperationArgSpec("package", "string", "Name des Ubuntu-Pakets.", required=True)],
                ["install", "tool", "package", "apt", "missing", "executable", "command not found", "capability"],
                mutates_state=True,
                controller_allowed=True,
            ),
            "request_from_owner": OperationSpec(
                "request_from_owner",
                "Den Owner per GitHub-Issue um etwas bitten, das Nexus selbst nicht bekommt "
                "(API-Key, mehr RAM/GPU, gesperrtes Paket, Repo-Einstellung, Budget). "
                "Höchstens 3 neue am Tag; ein offenes Issue mit gleichem Titel wird wiederverwendet.",
                [
                    OperationArgSpec("title", "string", "Kurz: was gebraucht wird.", required=True),
                    OperationArgSpec("body", "string", "Wofür, was schon versucht wurde, was der Owner tun soll.", required=True),
                ],
                ["owner", "request", "need", "blocked", "missing", "capability", "issue", "proposal", "key", "budget"],
                mutates_state=True,
                controller_allowed=True,
            ),
            "service_status": OperationSpec("service_status", "systemctl-Status eines systemd-Service (Standard: API-Service).", [service], ["service", "status", "systemd", "health"], mutates_state=False),
            "adoption_show": OperationSpec("adoption_show", "Sichere Kurzübersicht zu einer Adoption.", [adoption_id], ["adoption", "status", "summary", "inspect"], mutates_state=False),
            "github_status": OperationSpec(
                "github_status",
                "Aktueller GitHub-Lifecycle-Readiness-Status des laufenden Prozesses "
                "(Repository, Zielbranch, Vorhandensein von Credentials - niemals deren Werte).",
                tags=[
                    "github", "lifecycle", "readiness", "ready", "credentials",
                    "token", "identity", "repository", "remote", "runtime",
                    "configuration", "environment", "diagnostics", "blocked",
                ],
                mutates_state=False,
            ),
            "github_issue": OperationSpec(
                "github_issue",
                "Ein GitHub Issue read-only abrufen: Nummer, Titel, Beschreibung, Status, URL und Metadaten.",
                [issue_number],
                [
                    "github", "issue", "read", "fetch", "get", "details",
                    "specification", "acceptance criteria", "task",
                ],
                mutates_state=False,
            ),
            "github_mcp_tools": OperationSpec(
                "github_mcp_tools",
                "Die Tools des offiziellen GitHub-MCP-Servers auflisten (Name und Kurzbeschreibung), "
                "z. B. list_issues, issue_read, issue_write, add_issue_comment, list_commits (#41).",
                tags=["github", "mcp", "tools", "catalog", "issues", "commits", "repository"],
                mutates_state=False,
            ),
            "github_mcp_call": OperationSpec(
                "github_mcp_call",
                "Ein Tool des offiziellen GitHub-MCP-Servers aufrufen: Issues auflisten, lesen, "
                "anlegen, kommentieren, aktualisieren und schließen, Commits und Repository-Inhalte "
                "lesen. Standard-Repository: GITHUB_REPOSITORY (tire0011/agent-lab) (#41).",
                [
                    OperationArgSpec("tool", "string", "Name des GitHub-MCP-Tools, z. B. list_issues.", required=True),
                    OperationArgSpec(
                        "arguments",
                        "string",
                        'Tool-Argumente als JSON-Objekt, z. B. {"owner": "tire0011", "repo": "agent-lab", "state": "OPEN"}.',
                        default="{}",
                    ),
                ],
                [
                    "github", "mcp", "issue", "issues", "create issue", "comment", "close issue",
                    "update issue", "list issues", "commits", "repository", "pull request", "todo", "problem",
                ],
                mutates_state=True,
                controller_allowed=True,
            ),
            "problems_list": OperationSpec("problems_list", "Liste der letzten Probleme.", [limit20, since_timestamp], tags=["problems", "errors", "failures", "issues"], mutates_state=False),
            "web_research": OperationSpec(
                "web_research",
                "Begrenzte Web-Recherche über SearXNG mit robots.txt-Prüfung und Quellen-URLs.",
                [
                    OperationArgSpec("query", "string", "Konkrete Recherchefrage.", required=True),
                    OperationArgSpec("max_pages", "integer", "Maximal abzurufende Ergebnisseiten.", default=3, minimum=1, maximum=10),
                ],
                ["web", "search", "research", "internet", "sources", "current information"],
                mutates_state=False,
            ),
            "inbox_status": OperationSpec("inbox_status", "Status der Mailbox-Inbox.", tags=["mailbox", "inbox", "status", "messages"], mutates_state=False),
            "outbox_status": OperationSpec("outbox_status", "Neueste Dateien in der Mailbox-Outbox.", tags=["mailbox", "outbox", "status", "messages"], mutates_state=False),
        }

    def _audit(
        self,
        operation: str,
        args: dict[str, Any],
        result: OperationResult,
        *,
        caller: str,
        invocation_id: str | None,
    ) -> None:
        del caller  # never persist credentials or identifiers from the API key
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        logged_params = json.dumps(self._logged_params(args), sort_keys=True)
        self._conn.execute(
            """
            INSERT INTO operations_audit (id, timestamp, operation, args_json, success, duration_ms, error_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                timestamp,
                operation,
                logged_params,
                1 if result.success else 0,
                result.duration_ms,
                sanitized_reason(result.error_summary),
            ),
        )
        self._conn.execute(
            """
            INSERT INTO operation_log
                (id, timestamp, operation, parameters, success, result, error,
                 duration_ms, executor, invocation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                timestamp,
                operation,
                logged_params,
                1 if result.success else 0,
                json.dumps(result.to_dict(), sort_keys=True),
                sanitized_reason(result.error_summary),
                result.duration_ms,
                "operations_registry",
                invocation_id,
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

    def _resolve_service(self, args: dict[str, Any]) -> str:
        from modules.nexus_diagnostics import DiagnosticsError, resolve_service

        try:
            return resolve_service(self._service(args))
        except DiagnosticsError as exc:
            raise OperationValidationError(str(exc)) from exc

    def _since_minutes(self, args: dict[str, Any]) -> int | None:
        value = args.get("since_minutes")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise OperationValidationError("since_minutes muss eine ganze Zahl sein.")
        if value < 1 or value > 60 * 24 * 7:
            raise OperationValidationError("since_minutes muss zwischen 1 und 10080 liegen.")
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
        lines.extend(self._local_run_lines(experiment.id))
        return OperationResult("experiment_show", True, lines, 0)

    _LOCAL_RUN_EVENTS = ("local_run_metrics", "local_samples_result")

    def _local_run_lines(self, experiment_id: str) -> list[str]:
        """The last local coding run's cost and best-of-N outcome (#639), if any."""
        try:
            path = self.experiment_service.experiment_log_path(experiment_id)
            entries = central_logging_module.read_log_entries(path, tail_bytes=4_000_000)
        except Exception:  # an unreadable log only hides this optional section
            return []
        local = [e for e in entries if e.get("event") in self._LOCAL_RUN_EVENTS]
        last_run = max((i for i, e in enumerate(local) if e.get("event") == "local_run_metrics"), default=None)
        if last_run is None:
            return []
        start = max((i for i, e in enumerate(local[:last_run]) if e.get("event") == "local_run_metrics"),
                    default=-1) + 1
        shown = [local[last_run]] + local[start:last_run][-6:]
        return ["local_run:"] + [f"  {redact(str(e.get('message', '')))[:500]}" for e in shown]

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

    @staticmethod
    def _reflection_lines(stored: dict[str, Any]) -> list[str]:
        lines = [f"reflection {stored['experiment_id']} ({stored['created_at']}, {stored['model']})"]
        if stored.get("error"):
            lines.append(f"  error: {sanitized_reason(str(stored['error']))}")
        if not stored["suggestions"]:
            lines.append("  (no suggestions)")
        for suggestion in stored["suggestions"]:
            duplicate = suggestion.get("duplicate_of")
            lines.append(redact(
                f"  - {suggestion.get('title', '')}"
                + (f" [duplicate of: {duplicate}]" if duplicate else "")
                + f" | benefit: {suggestion.get('benefit', '')} | cost: {suggestion.get('cost', '')}"
                + f" | risk: {suggestion.get('risk', '')} | measure: {suggestion.get('measure', '')}"
            )[:500])
        return lines

    def _op_experiment_reflection(self, args: dict[str, Any]) -> OperationResult:
        from modules.experiment_reflection import get_reflection

        experiment = self._experiment(args)
        stored = get_reflection(experiment.id, db_path=self.db_path)
        if stored is None:
            return OperationResult("experiment_reflection", True, [f"no reflection stored for {experiment.id}"], 0)
        return OperationResult("experiment_reflection", True, self._reflection_lines(stored), 0)

    def _op_reflections_list(self, args: dict[str, Any]) -> OperationResult:
        from modules.experiment_reflection import list_reflections

        limit = self._limit(args, default=10, maximum=100)
        lines: list[str] = []
        for stored in list_reflections(limit, db_path=self.db_path):
            lines.extend(self._reflection_lines(stored))
        return OperationResult("reflections_list", True, lines or ["(no reflections stored)"], 0)

    def _op_local_replays_list(self, args: dict[str, Any]) -> OperationResult:
        from modules.local_shadow_replay import list_replays, variant_summary

        limit = self._limit(args, default=10, maximum=200)
        summary = variant_summary(db_path=self.db_path)
        lines: list[str] = []
        for stats in summary["variants"]:
            rate = stats["test_pass_rate"]
            full = stats["full_pass_rate"]
            lines.append(
                f"variant {stats['prompt_variant']}: {stats['replays']} replays, {stats['scored']} scored, "
                f"tests {stats['tests_passed']}/{stats['tests_total']}"
                + (f" ({rate:.0%})" if rate is not None else "")
                + f", all tests passed in {stats['full_passes']}/{stats['scored']}"
                + (f" ({full:.0%})" if full is not None else "")
                + f", no tests {stats['no_tests']}, errors {stats['errors']}"
                + (f", local too slow {stats['local_too_slow']}" if stats.get("local_too_slow") else "")
                + f", samples N={'/'.join(str(n) for n in stats.get('samples') or [1])}"
                + (f", median {stats['median_seconds']:.0f}s/task" if stats.get("median_seconds") is not None else "")
                + (f", median llama CPU {stats['median_cpu_seconds']:.0f}s"
                   if stats.get("median_cpu_seconds") is not None else "")
                + (f", peak RSS {stats['peak_rss_kb'] / 1024:.0f} MiB" if stats.get("peak_rss_kb") is not None else "")
            )
        for stats in summary.get("unit_variants", []):
            # #649: hunk-level units; completion = finished inside the worker time limit.
            completion, rate = stats.get("completion_rate"), stats.get("unit_pass_rate")
            lines.append(
                f"hunk variant {stats['prompt_variant']}: {stats['units']} units from "
                f"{stats.get('experiments', 0)} experiments, finished in time "
                f"{stats.get('completed', 0)}/{stats['units']}"
                + (f" ({completion:.0%})" if completion is not None else "")
                + f", timeouts {stats.get('timeouts', 0)}, errors {stats.get('errors', 0)}"
                + f", applied {stats['applied']}, compiled {stats['compiled']}, tests passed "
                f"{stats['tests_passed']}/{stats['units']}"
                + (f" ({rate:.0%})" if rate is not None else "")
                + f" ({stats.get('tested_units', 0)} with merged tests)"
                + f", overlap {stats['average_overlap']:.0%}, {stats['average_seconds']:.1f}s/unit, "
                f"prompt/cache tokens {stats['prompt_tokens']}/{stats['cached_tokens']}"
                + (f", running {stats['running']}" if stats.get("running") else "")
            )
        if summary["common"]:
            lines.append(f"on the {summary['common_experiments']} experiments scored under every variant:")
            for stats in summary["common"]:
                lines.append(
                    f"  {stats['prompt_variant']}: tests {stats['tests_passed']}/{stats['tests_total']}, "
                    f"all passed {stats['full_passes']}/{stats['scored']}"
                    + (f", median {stats['median_seconds']:.0f}s/task"
                       if stats.get("median_seconds") is not None else "")
                )
        for row in list_replays(limit, db_path=self.db_path):
            line = (
                f"replay {row['experiment_id']} [{row['prompt_variant']}] issue "
                f"{'#' + str(row['issue']) if row['issue'] else '-'}: {row['status']}, "
                f"tests {row['tests_passed']}/{row['tests_total']}, patch "
                f"{'applied' if row['patch_applied'] else 'none'}, rounds {row['rounds']}, "
                f"{row['seconds']:.0f}s"
            )
            if int(row.get("samples") or 1) > 1 or row.get("samples_requested") is not None:
                line += (f", samples N={int(row.get('samples') or 1)} "
                         f"({row.get('samples_requested') or 0} drawn, {row.get('samples_passed') or 0} passed)")
            if row.get("cpu_seconds") is not None:
                line += f", llama CPU {row['cpu_seconds']:.0f}s"
            if row.get("peak_rss_kb") is not None:
                line += f", peak RSS {row['peak_rss_kb'] / 1024:.0f} MiB"
            if row.get("tokens_per_second") is not None:
                line += f", {row['tokens_per_second']:.1f} tok/s"
            if row.get("error"):
                line += f" | {sanitized_reason(str(row['error']))}"
            lines.append(redact(line)[:500])
        return OperationResult("local_replays_list", True, lines or ["(no local replays stored)"], 0)

    def _op_branch_drift(self, args: dict[str, Any]) -> OperationResult:
        experiment = self._experiment(args)
        service = self.github_work_tracking_factory(
            db_path=self.db_path, repo_root=str(self.repo_root)
        )
        try:
            drift = service.branch_drift(experiment.id, experiment.worktree_path)
        finally:
            close = getattr(service, "close", None)
            if callable(close):
                close()
        lines = [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in drift.items()]
        return OperationResult("branch_drift", drift.get("status") == "ok", lines, 0,
                               error_summary=drift.get("reason") if drift.get("status") != "ok" else None)

    def _op_local_model_schema_check(self, args: dict[str, Any]) -> OperationResult:
        """Ask the model server whether it applies Nexus's own JSON schema.

        Changes no state, but costs two real completions.
        """
        if args:
            raise OperationValidationError(
                "local_model_schema_check akzeptiert keine Argumente."
            )
        from modules.llm_schema_probe import SHAPE_LABELS, probe, verdict

        started = time.perf_counter()
        base_url = (
            os.getenv("LOCAL_MODEL_URL")
            or os.getenv("LLAMA_CPP_BASE_URL")
            or "http://127.0.0.1:8080/v1"
        ).rstrip("/")
        model = (
            os.getenv("LOCAL_MODEL_NAME")
            or os.getenv("LLAMA_CPP_MODEL")
            or "qwen2.5-coder-7b-instruct"
        )
        results = probe(base_url=base_url, model=model, timeout=180.0, nexus_only=True)
        healthy, lines = verdict(results)
        detail = [
            f"{SHAPE_LABELS.get(key, key)}: {outcome} - {text}"
            for key, outcome, text in results
        ]
        return OperationResult(
            operation="local_model_schema_check",
            success=healthy,
            output_lines=bounded_lines(
                redact("\n".join([f"model: {model}", *detail, "", *lines])),
                max_lines=40,
            ),
            duration_ms=int((time.perf_counter() - started) * 1000),
            exit_code=0 if healthy else 1,
            error_summary=None if healthy else lines[0][:300],
        )

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

    def _op_vm_maintenance_report(self, args: dict[str, Any]) -> OperationResult:
        """Collect a bounded host report without changing package or service state."""
        if args:
            raise OperationValidationError("vm_maintenance_report akzeptiert keine Argumente.")

        started = time.perf_counter()
        lines = ["VM maintenance report (read-only)"]

        def run(label: str, command: list[str], *, max_lines: int = 8) -> list[str] | None:
            result = self._run_command(command, timeout=20)
            if not result.success:
                lines.append(f"{label}: unknown")
                return None
            return bounded_lines("\n".join(result.output_lines), max_lines=max_lines)

        updates = run("updates", ["apt", "list", "--upgradable"], max_lines=60)
        if updates is not None:
            packages = [
                line for line in updates
                if line.strip() and not line.startswith("Listing...") and not line.startswith("WARNING:")
            ]
            security = [line for line in packages if "security" in line.lower().split()[0]]
            lines.append(f"updates: {len(packages)} upgradable; {len(security)} security")
            if packages:
                lines.append("  " + ", ".join(line.split("/", 1)[0] for line in packages[:10]))

        failed_units = run(
            "failed units",
            ["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"],
        )
        if failed_units is not None:
            lines.append(f"failed units: {len(failed_units)}")
            lines.extend(f"  {line}" for line in failed_units)

        failed_timers = run(
            "failed timers",
            ["systemctl", "--failed", "--type=timer", "--no-legend", "--plain", "--no-pager"],
        )
        if failed_timers is not None:
            lines.append(f"failed timers: {len(failed_timers)}")
            lines.extend(f"  {line}" for line in failed_timers)

        for label, command in (
            ("disk", ["df", "-h", "--output=source,size,used,avail,pcent,target", "/"]),
            ("memory/swap", ["free", "-h"]),
            ("memory pressure", ["cat", "/proc/pressure/memory"]),
            ("largest processes", ["ps", "-eo", "pid,pmem,rss,comm", "--sort=-rss"]),
            (
                "certificate expiry",
                [
                    "find", "-L", "/etc/letsencrypt/live", "-name", "fullchain.pem",
                    "-exec", "openssl", "x509", "-in", "{}", "-noout", "-subject", "-enddate", ";",
                ],
            ),
        ):
            output = run(label, command, max_lines=6)
            if output is not None:
                lines.append(f"{label}:")
                lines.extend(f"  {line}" for line in output or ["none"])

        return OperationResult(
            operation="vm_maintenance_report",
            success=True,
            output_lines=bounded_lines("\n".join(lines), max_lines=80),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def _op_service_status(self, args: dict[str, Any]) -> OperationResult:
        service = self._resolve_service(args)
        result = self._run_command(["systemctl", "status", service, "--no-pager", "-n", "0"], timeout=15)
        result.operation = "service_status"
        return result

    def _op_service_restart(self, args: dict[str, Any]) -> OperationResult:
        from modules.service_operations import ServiceOperations

        if args.get("service") is None:
            raise OperationValidationError("service ist erforderlich (mailbox oder llm).")
        service = self._resolve_service(args)
        if service not in _RESTARTABLE_SERVICES:
            raise OperationValidationError(f"service_restart erlaubt nur mailbox oder llm, nicht {service}.")
        started = time.perf_counter()
        # Reuses the API restart; run in its own thread so a running event loop
        # of the caller does not matter.
        with ThreadPoolExecutor(max_workers=1) as pool:
            outcome = pool.submit(asyncio.run, ServiceOperations().restart(service)).result()
        status = outcome["results"][service]
        output_lines = [f"{service}: {status}"]
        if status == "restarted" and service == "llama-server":
            deadline = time.monotonic() + _LOCAL_MODEL_RESTART_TIMEOUT_SECONDS
            while True:
                readiness = self._op_local_model_status({})
                if readiness.success:
                    output_lines.append("local model: ready")
                    break
                if time.monotonic() >= deadline:
                    status = "local model did not come back before the readiness timeout"
                    output_lines.append(f"local model: {status}")
                    break
                time.sleep(_LOCAL_MODEL_RESTART_POLL_SECONDS)
        return OperationResult(
            "service_restart",
            status == "restarted",
            output_lines,
            int((time.perf_counter() - started) * 1000),
            error_summary=None if status == "restarted" else status,
        )

    def _active_experiment_ids(self) -> list[str]:
        placeholders = ", ".join("?" for _ in _ACTIVE_EXPERIMENT_STATUSES)
        rows = self._conn.execute(
            f"SELECT id FROM experiments WHERE status IN ({placeholders}) ORDER BY created_at",
            _ACTIVE_EXPERIMENT_STATUSES,
        ).fetchall()
        return [row["id"] for row in rows]

    def _op_install_tool(self, args: dict[str, Any]) -> OperationResult:
        package = str(args.get("package") or "")
        if not _PACKAGE_NAME.match(package):
            raise OperationValidationError("install_tool braucht genau einen Ubuntu-Paketnamen.")
        result = self._run_command([*INSTALL_TOOL_COMMAND, package], timeout=600)
        result.operation = "install_tool"
        if result.exit_code == 3:
            result.output_lines = [*result.output_lines, "denied: ask the owner with request_from_owner if you need it"]
        return result

    def _op_request_from_owner(self, args: dict[str, Any]) -> OperationResult:
        title = " ".join(str(args.get("title") or "").split())
        body = str(args.get("body") or "").strip()
        if not title or not body:
            raise OperationValidationError("request_from_owner braucht title und body.")
        started = time.perf_counter()
        service = self.github_work_tracking_factory(db_path=self.db_path, repo_root=str(self.repo_root))
        try:
            issue = service.open_need_issue(title, redact(body))
        except Exception as exc:  # noqa: BLE001
            return OperationResult("request_from_owner", False, [], int((time.perf_counter() - started) * 1000),
                                   error_summary=sanitized_reason(str(exc)))
        finally:
            close = getattr(service, "close", None)
            if callable(close):
                close()
        state = "reused" if issue.get("reused") else "opened"
        return OperationResult(
            "request_from_owner", True, [f"issue #{issue.get('number')} {state}: {issue.get('url')}"],
            int((time.perf_counter() - started) * 1000),
        )

    def _op_machine_reboot(self, args: dict[str, Any]) -> OperationResult:
        if args:
            raise OperationValidationError("machine_reboot akzeptiert keine Argumente.")
        active = self._active_experiment_ids()
        if active:
            # A reboot would kill the experiment mid-run; it would never
            # write a failed status (#188).
            raise OperationValidationError(
                "machine_reboot verweigert: Experiment aktiv (" + ", ".join(i[:8] for i in active[:5]) + ")."
            )
        result = self._run_command(list(MACHINE_REBOOT_COMMAND), timeout=15)
        result.operation = "machine_reboot"
        if result.success:
            result.output_lines = ["reboot scheduled in 60 seconds", *result.output_lines]
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
            # systemctl status returns 3 for an inactive unit, the normal state of
            # this oneshot after a successful run; healthy only if the output proves it.
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
        # Friendly names resolve through the diagnostic backend's table (#188);
        # unknown names are validated, so nothing shell-shaped reaches journalctl.
        service = self._resolve_service(args)
        limit = self._limit(args, default=50, maximum=500)
        command = ["journalctl", "-u", service, "-n", str(limit), "--no-pager"]
        since_minutes = self._since_minutes(args)
        if since_minutes is not None:
            command.append(f"--since=-{since_minutes}m")
        result = self._run_command(command, timeout=20)
        result.operation = "service_logs"
        return result

    # Nexus diagnostics (#188): the API's backend, rendered as operation output.

    def _diagnostics(self):
        from modules.nexus_diagnostics import NexusDiagnostics

        return NexusDiagnostics(
            db_path=self.db_path,
            log_dir=getattr(self.experiment_service, "experiment_log_dir", None),
        )

    def _reference(self, args: dict[str, Any]) -> str:
        value = args.get("reference")
        if not isinstance(value, str) or not value.strip():
            raise OperationValidationError("reference ist erforderlich.")
        return value.strip()

    def _run_diagnostic(
        self,
        operation: str,
        build: Callable[[Any], Any],
        render: Callable[[Any], list[str]] | None = None,
    ) -> OperationResult:
        from modules.nexus_diagnostics import DiagnosticsError, render_report

        started = time.perf_counter()
        diagnostics = None
        try:
            diagnostics = self._diagnostics()
            report = build(diagnostics)
            output_lines = (render or render_report)(report)
        except DiagnosticsError as exc:
            # A reference that resolves to nothing, or an unreadable database,
            # is a finding, not an operation fault.
            return OperationResult(
                operation=operation,
                success=False,
                output_lines=[],
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_summary=sanitized_reason(str(exc)),
            )
        finally:
            if diagnostics is not None:
                diagnostics.close()
        return OperationResult(
            operation=operation,
            success=True,
            output_lines=output_lines,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def _op_nexus_logs(self, args: dict[str, Any]) -> OperationResult:
        reference = self._reference(args)
        limit = self._limit(args, default=50, maximum=500)
        return self._run_diagnostic(
            "nexus_logs", lambda d: d.diagnose(reference, lines=limit)
        )

    def _op_nexus_status(self, args: dict[str, Any]) -> OperationResult:
        reference = self._reference(args)
        return self._run_diagnostic("nexus_status", lambda d: d.status_report(reference))

    def _op_nexus_llm_job(self, args: dict[str, Any]) -> OperationResult:
        from modules.nexus_diagnostics import LLM_JOB_VIEWS, render_llm_job

        job_id = args.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise OperationValidationError("job_id ist erforderlich.")
        view = args.get("view") or "summary"
        if view not in LLM_JOB_VIEWS:
            raise OperationValidationError("view muss summary, result oder prompt sein.")
        return self._run_diagnostic(
            "nexus_llm_job",
            lambda d: d.llm_job(job_id.strip()),
            lambda job: render_llm_job(job, view=view),
        )

    def _op_nexus_last_failure(self, args: dict[str, Any]) -> OperationResult:
        limit = self._limit(args, default=50, maximum=500)
        return self._run_diagnostic(
            "nexus_last_failure", lambda d: d.last_failure(lines=limit)
        )

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

    def _op_web_research(self, args: dict[str, Any]) -> OperationResult:
        from modules.web_research import WebResearchClient, WebResearchConfig

        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise OperationValidationError("query muss ein nicht-leerer Text sein.")
        if len(query) > 500:
            raise OperationValidationError("query darf höchstens 500 Zeichen lang sein.")
        client = WebResearchClient(WebResearchConfig.from_env())
        research = client.research(query.strip(), max_pages=args.get("max_pages", 3))
        return OperationResult(
            "web_research",
            research.success,
            research.lines,
            research.runtime_ms,
            error_summary=research.error,
            metrics=research.metrics,
        )

    def _op_problems_list(self, args: dict[str, Any]) -> OperationResult:
        from data_sources import list_problems
        limit = args.get("limit", 20)
        problems = list_problems(
            limit=limit,
            since=args.get("since"),
            experiment_db_path=self.db_path,
        )
        lines = [f"{p.id}: {p.summary}" for p in problems]
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
        from modules.mailbox.config import MailboxConfig

        outbox_dir = MailboxConfig.from_env().outbox_dir
        if not outbox_dir.exists():
            return OperationResult("outbox_status", True, ["outbox_dir does not exist"], 0)
        files = sorted(outbox_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
        lines = [f"{f.name}" for f in files]
        return OperationResult("outbox_status", True, lines, 0)

    def _op_github_status(self, args: dict[str, Any]) -> OperationResult:
        """Current GitHub lifecycle readiness as this process sees it (#172).

        Reports whether a repository and a credential are resolvable, never the
        credential itself.
        """
        if args:
            raise OperationValidationError("github_status akzeptiert keine Argumente.")
        started = time.perf_counter()
        try:
            service = self.github_work_tracking_factory(
                db_path=self.db_path,
                repo_root=str(self.repo_root),
            )
        except Exception as exc:  # noqa: BLE001 - a diagnostic must not raise
            return OperationResult(
                operation="github_status",
                success=False,
                output_lines=[],
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_summary=sanitized_reason(str(exc)),
            )
        try:
            readiness = service.readiness_error()
            repository = str(getattr(service, "repository", "") or "")
            token_present = bool(getattr(service, "token", None))
            lines = [
                f"repository: {repository or '(unresolved)'}",
                f"repository_env_set: {'yes' if os.environ.get('GITHUB_REPOSITORY') else 'no'}",
                f"token_present: {'yes' if token_present else 'no'}",
                f"github_token_env_set: {'yes' if os.environ.get('GITHUB_TOKEN') else 'no'}",
                f"gh_token_env_set: {'yes' if os.environ.get('GH_TOKEN') else 'no'}",
                f"target_branch: {getattr(service, 'target_branch', '') or '(unknown)'}",
                f"configured: {'yes' if getattr(service, 'configured', False) else 'no'}",
                f"lifecycle_ready: {'no' if readiness else 'yes'}",
            ]
            if readiness:
                lines.append(f"readiness_error: {sanitized_reason(readiness)}")
            return OperationResult(
                operation="github_status",
                success=True,
                output_lines=bounded_lines("\n".join(lines), max_lines=20),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - a diagnostic must not raise
            return OperationResult(
                operation="github_status",
                success=False,
                output_lines=[],
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_summary=sanitized_reason(str(exc)),
            )
        finally:
            close = getattr(service, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    logger.warning("github_status: could not close tracking service", exc_info=True)

    def _op_github_mcp_tools(self, args: dict[str, Any]) -> OperationResult:
        if args:
            raise OperationValidationError("github_mcp_tools akzeptiert keine Argumente.")
        from modules import github_mcp

        started = time.perf_counter()
        tools = github_mcp.list_tools(self.github_mcp_config_factory())
        lines = []
        for tool in sorted(tools, key=lambda item: item.name):
            description = (getattr(tool, "description", "") or "").strip().splitlines()
            lines.append(f"{tool.name}: {description[0][:160] if description else ''}".rstrip(": "))
        return OperationResult(
            operation="github_mcp_tools",
            success=True,
            output_lines=lines[:300],
            duration_ms=int((time.perf_counter() - started) * 1000),
            metrics={"tool_count": len(tools)},
        )

    def _op_github_mcp_call(self, args: dict[str, Any]) -> OperationResult:
        unexpected = sorted(set(args) - {"tool", "arguments"})
        if unexpected:
            raise OperationValidationError(
                f"github_mcp_call kennt diese Argumente nicht: {', '.join(unexpected)}"
            )
        tool = args.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise OperationValidationError("tool muss ein nicht leerer Tool-Name sein.")
        raw_arguments = args.get("arguments")
        if raw_arguments is None or raw_arguments == "":
            arguments: Any = {}
        elif isinstance(raw_arguments, dict):
            arguments = raw_arguments
        elif isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments)
            except ValueError as exc:
                raise OperationValidationError("arguments muss ein JSON-Objekt sein.") from exc
        else:
            arguments = None
        if not isinstance(arguments, dict):
            raise OperationValidationError("arguments muss ein JSON-Objekt sein.")
        from modules import github_mcp

        started = time.perf_counter()
        result = github_mcp.call_tool(tool.strip(), arguments, self.github_mcp_config_factory())
        decoded = result.json()
        text = (
            json.dumps(decoded, ensure_ascii=False, indent=1)
            if decoded is not None
            else result.text
        )
        # Head, not tail: a JSON listing starts with what matters.
        lines = [redact(line)[:500] for line in text.splitlines()[:_GITHUB_MCP_MAX_LINES]]
        return OperationResult(
            operation="github_mcp_call",
            success=not result.is_error,
            output_lines=lines,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error_summary=sanitized_reason(result.text[:500]) if result.is_error else None,
            metrics={"tool": tool.strip()},
        )

    def _op_github_issue(self, args: dict[str, Any]) -> OperationResult:
        unexpected = sorted(set(args) - {"issue_number"})
        if unexpected:
            raise OperationValidationError(
                f"github_issue kennt diese Argumente nicht: {', '.join(unexpected)}"
            )
        issue_number = args.get("issue_number")
        if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number < 1:
            raise OperationValidationError("issue_number muss eine positive ganze Zahl sein.")

        started = time.perf_counter()
        service = self.github_work_tracking_factory(
            db_path=self.db_path,
            repo_root=str(self.repo_root),
        )
        try:
            issue = service.get_issue(issue_number)
            metadata = {
                key: issue.get(key)
                for key in (
                    "number", "title", "state", "url", "author", "labels",
                    "assignees", "created_at", "updated_at", "body_truncated",
                )
            }
            lines = [
                f"issue: {redact(json.dumps(metadata, ensure_ascii=False, sort_keys=True))}"
            ]
            body = redact(str(issue.get("body") or ""))
            for index in range(0, len(body), 450):
                lines.append(f"body[{index // 450 + 1}]: {body[index:index + 450]}")
            if not body:
                lines.append("body: ")
            return OperationResult(
                operation="github_issue",
                success=True,
                output_lines=lines[:80],
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        finally:
            close = getattr(service, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    logger.warning("github_issue: could not close tracking service", exc_info=True)
