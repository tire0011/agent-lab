from fastapi import APIRouter, Depends, Header, Query
from models import DiagnosticActivityResponse, PublicActivityResponse
from modules.experiment_service import ExperimentService

from api import LIMIT_MIN, app, get_experiment_service
from modules import redaction
from modules.experiment_service import ExperimentNotFoundError
from modules.mailbox.task_service import ApprovalNotFoundError
import api

router = APIRouter(dependency_overrides_provider=app)


ACTIVITY_DEFAULT_LIMIT = 50
ACTIVITY_MAX_LIMIT = 200


def _diagnostic_lines(raw: str | None) -> list[str]:
    """Return bounded, redacted test diagnostics; never return raw output."""
    return redaction.bounded_lines(raw)


def _sanitized_rejection_reason(raw: str | None) -> str | None:
    """Bounded, redacted single-line rejection/error reason for diagnostics."""
    return redaction.sanitized_reason(raw)



@router.get("/activity/public", response_model=PublicActivityResponse)
def list_public_activity(
    service: ExperimentService = Depends(get_experiment_service),
) -> PublicActivityResponse:
    result = []
    for entry in service.list_activity(limit=100):
        try:
            experiment = service.get_experiment(entry["entity_id"])
        except ExperimentNotFoundError:
            continue
        status = entry.get("to_status") or experiment.status
        result.append({
            "timestamp": entry["timestamp"],
            "phase": api._public_phase(status),
            "status": status,
            "message": f"Experiment phase is {api._public_phase(status)}.",
            "mailbox_task_id": api._mailbox_task_id(service, experiment.id),
            "experiment_id": experiment.id,
            "failure_reason": api._failure_reason(experiment) if status in api._PUBLIC_PHASES and api._public_phase(status) in {"failed", "needs_review"} else None,
            "test_exit_code": experiment.test_exit_code,
            "changed_files": api._changed_file_names(experiment.git_status),
            "test_summary": api._test_summary(experiment),
        })
    return PublicActivityResponse(count=len(result), activity=result)


@router.get("/activity", response_model=DiagnosticActivityResponse)
def list_activity(
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=ACTIVITY_DEFAULT_LIMIT, ge=LIMIT_MIN, le=ACTIVITY_MAX_LIMIT),
    experiment_id: str | None = Query(default=None),
    mailbox_task_id: str | None = Query(default=None),
    service: ExperimentService = Depends(get_experiment_service),
) -> DiagnosticActivityResponse:
    api.authorize(x_api_key)
    if mailbox_task_id:
        approval_service = api._resolve_mailbox_task_service()
        try:
            approval = approval_service.get(mailbox_task_id)
        except ApprovalNotFoundError:
            return DiagnosticActivityResponse(count=0, activity=[])
        if experiment_id and approval.experiment_id != experiment_id:
            return DiagnosticActivityResponse(count=0, activity=[])
        experiment_id = approval.experiment_id
        if not experiment_id:
            return DiagnosticActivityResponse(count=0, activity=[])
    entries = service.list_activity(limit=limit, experiment_id=experiment_id)
    diagnostics = []
    for entry in entries:
        experiment = service.get_experiment(entry["entity_id"])
        diagnostics.append({
            **entry,
            "message": None,
            "mailbox_task_id": api._mailbox_task_id(service, experiment.id),
            "phase": api._public_phase(entry.get("to_status") or experiment.status),
            "policy_rule": experiment.policy_rule,
            "rejection_reason": _sanitized_rejection_reason(experiment.error),
            "test_command": experiment.test_command,
            "test_exit_code": experiment.test_exit_code,
            "changed_files": api._changed_file_names(experiment.git_status),
            "test_output_lines": _diagnostic_lines(experiment.test_output),
        })
    return DiagnosticActivityResponse(count=len(diagnostics), activity=diagnostics)
