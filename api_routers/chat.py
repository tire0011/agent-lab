from fastapi import APIRouter, Header
from models import ChatJobResponse, ChatRequest

from api import app

router = APIRouter(dependency_overrides_provider=app)


@router.post("/chat", response_model=ChatJobResponse, status_code=202)
def chat(
    request: ChatRequest,
    x_api_key: str | None = Header(default=None),
) -> ChatJobResponse:
    authorize(x_api_key)
    service = _resolve_nexus_chat_job_service()
    try:
        job = service.submit(request.message, session_id=request.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ChatJobResponse(job_id=job.id, status=job.status, session_id=job.session_id)


@router.get("/chat/jobs/{job_id}", response_model=ChatJobResponse)
def get_chat_job(
    job_id: str,
    x_api_key: str | None = Header(default=None),
) -> ChatJobResponse:
    authorize(x_api_key)
    service = _resolve_nexus_chat_job_service()
    job = service.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="chat job not found")
    if job.status == STATUS_SUCCEEDED and job.result is not None:
        result = job.result
        return ChatJobResponse(
  job_id=job.id,
  status=job.status,
  reply=result.reply,
  intent=result.action,
  session_id=result.session_id,
  action=result.action,
  experiment_id=result.experiment_id,
        )
    return ChatJobResponse(
        job_id=job.id,
        status=job.status,
        session_id=job.session_id,
        error=job.error if job.status == STATUS_FAILED else None,
    )
