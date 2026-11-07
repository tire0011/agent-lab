from fastapi import APIRouter, Depends, Header
from models import (
    MaintenanceAuditResponse,
    MaintenanceExecuteRequest,
    MaintenanceExecuteResponse,
    MaintenanceStatusResponse,
)
from modules.maintenance_service import MaintenanceService

from api import app, get_maintenance_service

router = APIRouter(dependency_overrides_provider=app)


# All /maintenance/* endpoints require the normal X-Api-Key, a second
# X-Maintenance-Token header, MAINTENANCE_ENABLED=true, and UTC time before
# MAINTENANCE_EXPIRY (if set). Temporary remote-maintenance bridge - disable it
# again after use (unset MAINTENANCE_ENABLED).


@router.get("/maintenance/status", response_model=MaintenanceStatusResponse)
def get_maintenance_status(
    x_api_key: str | None = Header(default=None),
    service: MaintenanceService = Depends(get_maintenance_service),
) -> MaintenanceStatusResponse:
    """Return whether the maintenance service is currently enabled."""
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    expiry = os.environ.get("MAINTENANCE_EXPIRY") or None
    return MaintenanceStatusResponse(
        enabled=service.is_enabled(),
        expiry=expiry,
    )


@router.post("/maintenance/execute", response_model=MaintenanceExecuteResponse)
def maintenance_execute(
    body: MaintenanceExecuteRequest,
    x_api_key: str | None = Header(default=None),
    x_maintenance_token: str | None = Header(default=None),
    service: MaintenanceService = Depends(get_maintenance_service),
) -> MaintenanceExecuteResponse:
    """Execute a named maintenance operation.

    Returns the result as a JSON object; background operations (``run_router_test``,
    ``run_tests``) return a ``task_id`` pollable via ``router_task_status`` /
    ``test_task_status``.
    """
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        result = service.execute(body.operation, x_maintenance_token, body.args)
    except MaintenanceDisabledError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except MaintenanceAuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except MaintenanceOperationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return MaintenanceExecuteResponse(operation=body.operation, result=result)


@router.get("/maintenance/audit", response_model=MaintenanceAuditResponse)
def get_maintenance_audit(
    x_api_key: str | None = Header(default=None),
    service: MaintenanceService = Depends(get_maintenance_service),
) -> MaintenanceAuditResponse:
    """Return the in-memory maintenance audit log."""
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    entries = service.audit_log
    return MaintenanceAuditResponse(
        entries=[
            {"timestamp": e.timestamp, "operation": e.operation,
             "args": e.args, "outcome": e.outcome}
            for e in entries
        ],
        total=len(entries),
    )
