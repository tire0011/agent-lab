from fastapi import APIRouter, Depends, Header, Query
from models import (
    GitCommitPushRequest,
    LlamaModelTestRequest,
    OperationCatalogEntry,
    OperationChainRequest,
    OperationChainResult,
    OperationResponse,
    OperationsCatalogResponse,
    OperationsExecuteRequest,
    OperationsExecuteResponse,
    ServiceRestartRequest,
    SetConfigRequest,
)
from modules.operation_chain_service import OperationChainService
from modules.operations_executor import OperationsExecutor
from modules.operations_registry import OperationsRegistry

from api import (
    LIMIT_MIN,
    app,
    get_operation_chain_service,
    get_operations_executor,
    get_operations_service,
)

registry_router = APIRouter(dependency_overrides_provider=app)
chains_router = APIRouter(dependency_overrides_provider=app)
executor_router = APIRouter(dependency_overrides_provider=app)


OPERATION_CHAINS_DEFAULT_LIMIT = 20
OPERATION_CHAINS_MAX_LIMIT = 100


@registry_router.get("/operations/catalog", response_model=OperationsCatalogResponse)
def get_operations_catalog(
    x_api_key: str | None = Header(default=None),
    operations_service: OperationsRegistry = Depends(get_operations_service),
) -> OperationsCatalogResponse:
    authorize(x_api_key)
    operations = [OperationCatalogEntry(**entry.to_dict()) for entry in operations_service.catalog()]
    return OperationsCatalogResponse(count=len(operations), operations=operations)


@registry_router.post("/operations/execute", response_model=OperationsExecuteResponse)
def execute_operation(
    request: OperationsExecuteRequest,
    x_api_key: str | None = Header(default=None),
    operations_service: OperationsRegistry = Depends(get_operations_service),
) -> OperationsExecuteResponse:
    authorize(x_api_key)
    try:
        result = operations_service.execute(
            request.operation,
            request.args.model_dump(exclude_none=True),
            invocation_id=request.invocation_id,
        )
    except OperationValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return OperationsExecuteResponse(**result.to_dict())


# Operation Chain endpoints (authenticated, sequential operation execution).


@chains_router.post("/operations/chains", response_model=OperationChainResult, status_code=202)
def start_operation_chain(
    request: OperationChainRequest,
    x_api_key: str | None = Header(default=None),
    service: OperationChainService = Depends(get_operation_chain_service),
) -> OperationChainResult:
    authorize(x_api_key)
    try:
        result = service.start_chain(request)
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid chain: {str(exc)}")


@chains_router.get("/operations/chains/{request_id}", response_model=OperationChainResult)
def get_operation_chain(
    request_id: str,
    x_api_key: str | None = Header(default=None),
    service: OperationChainService = Depends(get_operation_chain_service),
) -> OperationChainResult:
    authorize(x_api_key)
    result = service.get_chain(request_id)
    if not result:
        raise HTTPException(status_code=404, detail="Chain not found")
    return result


@chains_router.get("/operations/chains", response_model=list[OperationChainResult])
def list_operation_chains(
    limit: int = Query(default=OPERATION_CHAINS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=OPERATION_CHAINS_MAX_LIMIT),
    x_api_key: str | None = Header(default=None),
    service: OperationChainService = Depends(get_operation_chain_service),
) -> list[OperationChainResult]:
    authorize(x_api_key)
    return service.list_chains(limit=limit)



def _op_response(result, operation: str) -> OperationResponse:
    from modules.operations_executor import OperationResult
    return OperationResponse(
        success=result.success,
        status=result.status,
        operation=operation,
        timestamp=result.timestamp,
        data=result.data,
        error=result.error,
        duration_ms=result.duration_ms,
    )



@executor_router.post("/operations/git/pull", response_model=OperationResponse)
async def operations_git_pull(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("git/pull", {})
    return _op_response(result, "git/pull")


@executor_router.get("/operations/git/status", response_model=OperationResponse)
async def operations_git_status(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("git/status", {})
    return _op_response(result, "git/status")


@executor_router.post("/operations/git/commit-and-push", response_model=OperationResponse)
async def operations_git_commit_push(
    request: GitCommitPushRequest,
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute(
        "git/commit-and-push",
        {"message": request.message, "files": request.files},
    )
    return _op_response(result, "git/commit-and-push")



@executor_router.post("/operations/services/restart", response_model=OperationResponse)
async def operations_services_restart(
    request: ServiceRestartRequest,
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("services/restart", {"service": request.service})
    return _op_response(result, "services/restart")


@executor_router.get("/operations/services/status", response_model=OperationResponse)
async def operations_services_status(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("services/status", {})
    return _op_response(result, "services/status")


@executor_router.post("/operations/services/llama/reload-model", response_model=OperationResponse)
async def operations_llama_reload_model(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("services/llama/reload-model", {})
    return _op_response(result, "services/llama/reload-model")



@executor_router.get("/operations/logs/tail", response_model=OperationResponse)
async def operations_logs_tail(
    service: str = Query(...),
    lines: int = Query(default=50, ge=1, le=500),
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("logs/tail", {"service": service, "lines": lines})
    return _op_response(result, "logs/tail")


@executor_router.get("/operations/logs/errors", response_model=OperationResponse)
async def operations_logs_errors(
    service: str = Query(...),
    since_minutes: int = Query(default=60, ge=1, le=1440),
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute(
        "logs/errors", {"service": service, "since_minutes": since_minutes}
    )
    return _op_response(result, "logs/errors")



@executor_router.post("/operations/healing/fix-locks", response_model=OperationResponse)
async def operations_healing_fix_locks(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("healing/fix-locks", {})
    return _op_response(result, "healing/fix-locks")


@executor_router.post("/operations/healing/cleanup-temps", response_model=OperationResponse)
async def operations_healing_cleanup_temps(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("healing/cleanup-temps", {})
    return _op_response(result, "healing/cleanup-temps")


@executor_router.get("/operations/healing/diagnose", response_model=OperationResponse)
async def operations_healing_diagnose(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("healing/diagnose", {})
    return _op_response(result, "healing/diagnose")



@executor_router.get("/operations/models/status", response_model=OperationResponse)
async def operations_models_status(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("models/status", {})
    return _op_response(result, "models/status")


@executor_router.post("/operations/models/test", response_model=OperationResponse)
async def operations_models_test(
    request: LlamaModelTestRequest,
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute(
        "models/test", {"model": request.model, "prompt": request.prompt}
    )
    return _op_response(result, "models/test")



@executor_router.get("/operations/context/env", response_model=OperationResponse)
async def operations_context_env(
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute("context/env", {})
    return _op_response(result, "context/env")


@executor_router.post("/operations/context/set-config", response_model=OperationResponse)
async def operations_context_set_config(
    request: SetConfigRequest,
    x_api_key: str | None = Header(default=None),
    executor: OperationsExecutor = Depends(get_operations_executor),
) -> OperationResponse:
    authorize(x_api_key)
    result = await executor.execute(
        "context/set-config", {"key": request.key, "value": request.value}
    )
    return _op_response(result, "context/set-config")
