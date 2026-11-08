from fastapi import APIRouter, Depends, Header, Query
from models import (
    CodexTaskCreateRequest,
    CodexTaskListResponse,
    CodexTaskResponse,
    CodexTaskSummary,
)
from modules.codex_task_service import CodexTaskService

from api import LIMIT_MIN, app, get_codex_service
from fastapi import HTTPException
from modules.codex_task_service import TaskNotFoundError
from modules.codex_task_service import TaskNotRunnableError
import api

router = APIRouter(dependency_overrides_provider=app)


# Codex task API: clients never choose the working directory or sandbox mode -
# both are hard-coded so tasks stay read-only and confined to the repository root,
# regardless of the request payload.
CODEX_WORKING_DIR = "/agent"
CODEX_SANDBOX = "read-only"
CODEX_DEFAULT_TIMEOUT_SECONDS = 120
CODEX_MAX_TIMEOUT_SECONDS = 120
CODEX_LIST_DEFAULT_LIMIT = 20
CODEX_LIST_MAX_LIMIT = 100
CODEX_LIST_PROMPT_PREVIEW_CHARS = 80



@router.post("/codex/tasks", response_model=CodexTaskResponse, status_code=201)
def create_codex_task(
    request: CodexTaskCreateRequest,
    x_api_key: str | None = Header(default=None),
    service: CodexTaskService = Depends(get_codex_service),
) -> CodexTaskResponse:
    api.authorize(x_api_key)

    prompt = request.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt must not be empty")

    timeout_seconds = min(
        request.timeout_seconds or CODEX_DEFAULT_TIMEOUT_SECONDS,
        CODEX_MAX_TIMEOUT_SECONDS,
    )

    task = service.create_task(
        prompt=prompt,
        working_dir=CODEX_WORKING_DIR,
        sandbox=CODEX_SANDBOX,
        timeout_seconds=timeout_seconds,
    )
    return CodexTaskResponse(**task.to_dict())


def _prompt_preview(prompt: str, max_chars: int = CODEX_LIST_PROMPT_PREVIEW_CHARS) -> str:
    stripped = prompt.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[:max_chars].rstrip() + "…"


@router.get("/codex/tasks", response_model=CodexTaskListResponse)
def list_codex_tasks(
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=CODEX_LIST_DEFAULT_LIMIT, ge=LIMIT_MIN, le=CODEX_LIST_MAX_LIMIT),
    service: CodexTaskService = Depends(get_codex_service),
) -> CodexTaskListResponse:
    api.authorize(x_api_key)

    tasks = service.list_tasks(limit=limit)
    return CodexTaskListResponse(
        count=len(tasks),
        tasks=[
            CodexTaskSummary(
                **task.to_dict(),
                prompt_preview=_prompt_preview(task.prompt),
            )
            for task in tasks
        ],
    )


@router.get("/codex/tasks/{task_id}", response_model=CodexTaskResponse)
def get_codex_task(
    task_id: str,
    x_api_key: str | None = Header(default=None),
    service: CodexTaskService = Depends(get_codex_service),
) -> CodexTaskResponse:
    api.authorize(x_api_key)

    try:
        task = service.get_task(task_id)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="task not found")
    return CodexTaskResponse(**task.to_dict())


@router.post("/codex/tasks/{task_id}/run", response_model=CodexTaskResponse)
def run_codex_task(
    task_id: str,
    x_api_key: str | None = Header(default=None),
    service: CodexTaskService = Depends(get_codex_service),
) -> CodexTaskResponse:
    api.authorize(x_api_key)

    try:
        task = service.run_task(task_id)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="task not found")
    except TaskNotRunnableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        # Worker-level failures are already captured as a stored task status; anything
        # reaching here is unexpected - never leak tracebacks/secrets.
        raise HTTPException(status_code=500, detail="internal error running codex task")
    return CodexTaskResponse(**task.to_dict())
