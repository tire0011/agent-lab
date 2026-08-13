import os
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import chat as chat_module
from data_sources import InvalidIdeaTextError, add_idea, list_ideas, list_problems
from modules.codex_task_service import (
    CodexTaskService,
    TaskNotFoundError,
    TaskNotRunnableError,
)
from modules.mailbox_task_service import (
    ApprovalNotFoundError,
    ApprovalStateError,
    MailboxTaskService,
    MailboxTaskWorker,
)
from models import (
    ChatRequest,
    ChatResponse,
    CodexTaskCreateRequest,
    CodexTaskListResponse,
    CodexTaskResponse,
    CodexTaskSummary,
    HealthResponse,
    Idea,
    IdeaCreateRequest,
    IdeasResponse,
    MailboxTaskApproval,
    MailboxTaskApprovalsResponse,
    MailboxTaskRejectRequest,
    Problem,
    ProblemsResponse,
    StatusResponse,
)

# ---------------------------------------------------------------------------
# Codex task API: fixed server-side execution boundaries.
#
# Clients never choose the working directory or sandbox mode; both are
# hard-coded here so Codex tasks started through this API stay read-only
# and confined to the repository root, regardless of request payload.
# ---------------------------------------------------------------------------
CODEX_WORKING_DIR = "/agent"
CODEX_SANDBOX = "read-only"
CODEX_DEFAULT_TIMEOUT_SECONDS = 120
CODEX_MAX_TIMEOUT_SECONDS = 120
CODEX_LIST_DEFAULT_LIMIT = 20
CODEX_LIST_MAX_LIMIT = 100
CODEX_LIST_PROMPT_PREVIEW_CHARS = 80


@lru_cache(maxsize=1)
def get_codex_service() -> CodexTaskService:
    """Singleton CodexTaskService for the process.

    Overridable in tests via ``app.dependency_overrides[get_codex_service]``.
    """
    return CodexTaskService()


@lru_cache(maxsize=1)
def get_mailbox_task_service() -> MailboxTaskService:
    """Singleton MailboxTaskService for the process.

    Overridable in tests via ``app.dependency_overrides[get_mailbox_task_service]``.
    """
    from modules.experiment_service import ExperimentService
    exp_service = ExperimentService()
    return MailboxTaskService(experiment_service=exp_service)


def _resolve_codex_service() -> CodexTaskService:
    """Look up the (possibly test-overridden) Codex service for startup use.

    Direct calls to ``get_codex_service()`` would bypass FastAPI's
    ``dependency_overrides`` used by tests, so lifespan startup goes through
    this helper instead.
    """
    override = app.dependency_overrides.get(get_codex_service)
    if override is not None:
        return override()
    return get_codex_service()


def _resolve_mailbox_task_service() -> MailboxTaskService:
    override = app.dependency_overrides.get(get_mailbox_task_service)
    if override is not None:
        return override()
    return get_mailbox_task_service()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Mark orphaned 'running' Codex tasks as failed exactly once at startup.
    # Never restarts execution and never starts a Codex process.
    _resolve_codex_service().mark_interrupted_tasks_failed()
    mts = _resolve_mailbox_task_service()
    mts.recover_after_restart()
    worker = MailboxTaskWorker(mts)
    worker.start()
    yield


app = FastAPI(lifespan=lifespan)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

API_KEY = os.environ["AGENT_API_KEY"]


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


def authorize(x_api_key: str | None):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/secure-health")
def secure_health(x_api_key: str | None = Header(default=None)):
    authorize(x_api_key)
    return {"status": "ok", "auth": "ok"}


@app.get("/status", response_model=StatusResponse)
def status() -> StatusResponse:
    return StatusResponse(**chat_module.build_status())


@app.get("/problems", response_model=ProblemsResponse)
def problems() -> ProblemsResponse:
    found: list[Problem] = list_problems()
    return ProblemsResponse(count=len(found), problems=found)


@app.get("/ideas", response_model=IdeasResponse)
def ideas() -> IdeasResponse:
    found: list[Idea] = list_ideas()
    return IdeasResponse(count=len(found), ideas=found)


@app.post("/ideas", response_model=Idea, status_code=201)
def create_idea(
    request: IdeaCreateRequest,
    x_api_key: str | None = Header(default=None),
) -> Idea:
    authorize(x_api_key)

    try:
        return add_idea(request.text)
    except InvalidIdeaTextError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message must not be empty")

    reply, intent = chat_module.answer(message)
    return ChatResponse(reply=reply, intent=intent)


# ---------------------------------------------------------------------------
# Codex task endpoints (authenticated, synchronous, read-only sandbox only)
# ---------------------------------------------------------------------------


@app.post("/codex/tasks", response_model=CodexTaskResponse, status_code=201)
def create_codex_task(
    request: CodexTaskCreateRequest,
    x_api_key: str | None = Header(default=None),
    service: CodexTaskService = Depends(get_codex_service),
) -> CodexTaskResponse:
    authorize(x_api_key)

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


@app.get("/codex/tasks", response_model=CodexTaskListResponse)
def list_codex_tasks(
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=CODEX_LIST_DEFAULT_LIMIT, ge=1, le=CODEX_LIST_MAX_LIMIT),
    service: CodexTaskService = Depends(get_codex_service),
) -> CodexTaskListResponse:
    authorize(x_api_key)

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


@app.get("/codex/tasks/{task_id}", response_model=CodexTaskResponse)
def get_codex_task(
    task_id: str,
    x_api_key: str | None = Header(default=None),
    service: CodexTaskService = Depends(get_codex_service),
) -> CodexTaskResponse:
    authorize(x_api_key)

    try:
        task = service.get_task(task_id)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="task not found")
    return CodexTaskResponse(**task.to_dict())


@app.post("/codex/tasks/{task_id}/run", response_model=CodexTaskResponse)
def run_codex_task(
    task_id: str,
    x_api_key: str | None = Header(default=None),
    service: CodexTaskService = Depends(get_codex_service),
) -> CodexTaskResponse:
    authorize(x_api_key)

    try:
        task = service.run_task(task_id)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="task not found")
    except TaskNotRunnableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        # Worker-level failures are already captured as a stored task status
        # by CodexTaskService.run_task. Anything reaching here is an
        # unexpected internal error; never leak tracebacks/secrets.
        raise HTTPException(status_code=500, detail="internal error running codex task")
    return CodexTaskResponse(**task.to_dict())


# ---------------------------------------------------------------------------
# Mailbox task approval endpoints (authenticated)
# ---------------------------------------------------------------------------


def _approval_to_response(approval) -> MailboxTaskApproval:
    return MailboxTaskApproval(**approval.to_dict())


@app.get("/mailbox/tasks", response_model=MailboxTaskApprovalsResponse)
def list_mailbox_tasks(
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    service: MailboxTaskService = Depends(get_mailbox_task_service),
) -> MailboxTaskApprovalsResponse:
    authorize(x_api_key)
    tasks = service.list_recent(limit=limit)
    return MailboxTaskApprovalsResponse(
        count=len(tasks),
        tasks=[_approval_to_response(t) for t in tasks],
    )


@app.get("/mailbox/tasks/{approval_id}", response_model=MailboxTaskApproval)
def get_mailbox_task(
    approval_id: str,
    x_api_key: str | None = Header(default=None),
    service: MailboxTaskService = Depends(get_mailbox_task_service),
) -> MailboxTaskApproval:
    authorize(x_api_key)
    try:
        approval = service.get(approval_id)
    except ApprovalNotFoundError:
        raise HTTPException(status_code=404, detail="approval not found")
    return _approval_to_response(approval)


@app.post("/mailbox/tasks/{approval_id}/approve", response_model=MailboxTaskApproval)
def approve_mailbox_task(
    approval_id: str,
    x_api_key: str | None = Header(default=None),
    service: MailboxTaskService = Depends(get_mailbox_task_service),
) -> MailboxTaskApproval:
    authorize(x_api_key)
    try:
        approval = service.approve(approval_id)
    except ApprovalNotFoundError:
        raise HTTPException(status_code=404, detail="approval not found")
    except ApprovalStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _approval_to_response(approval)


@app.post("/mailbox/tasks/{approval_id}/reject", response_model=MailboxTaskApproval)
def reject_mailbox_task(
    approval_id: str,
    request: MailboxTaskRejectRequest,
    x_api_key: str | None = Header(default=None),
    service: MailboxTaskService = Depends(get_mailbox_task_service),
) -> MailboxTaskApproval:
    authorize(x_api_key)
    try:
        approval = service.reject(approval_id, request.reason)
    except ApprovalNotFoundError:
        raise HTTPException(status_code=404, detail="approval not found")
    except ApprovalStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _approval_to_response(approval)
