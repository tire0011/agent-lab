import os
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from agent import run_agent
from run_store import init_db, insert_problem, list_problems

router = APIRouter()


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)


class MessageResponse(BaseModel):
    response: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    tool_calls: int


class ProblemRequest(BaseModel):
    severity: str = Field(default="warning", pattern="^(info|warning|error|critical)$")
    title: str = Field(min_length=1, max_length=200)
    details: str = Field(default="", max_length=20_000)
    source: str = Field(default="agent", min_length=1, max_length=100)


class ProblemResponse(BaseModel):
    id: int
    status: str = "reported"


def authorize(x_api_key: str | None) -> None:
    api_key = os.getenv("AGENT_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="API authentication is not configured")
    if x_api_key != api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/secure-health")
def secure_health(x_api_key: str | None = Header(default=None)):
    authorize(x_api_key)
    return {"status": "ok", "auth": "ok"}


@router.get("/status")
def status(x_api_key: str | None = Header(default=None)):
    authorize(x_api_key)
    return {
        "status": "ok",
        "agent": "available",
        "model": os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o"),
    }


@router.post("/messages", response_model=MessageResponse)
async def send_message(
    request: MessageRequest,
    x_api_key: str | None = Header(default=None),
):
    authorize(x_api_key)

    try:
        (
            response,
            input_tokens,
            output_tokens,
            total_tokens,
            model,
            tool_trace,
        ) = await run_agent(request.message.strip())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Agent execution failed: {exc}") from exc

    return MessageResponse(
        response=response,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        tool_calls=len(tool_trace),
    )


@router.post("/problems", response_model=ProblemResponse, status_code=201)
def report_problem(
    request: ProblemRequest,
    x_api_key: str | None = Header(default=None),
):
    authorize(x_api_key)
    db = init_db()
    try:
        problem_id = insert_problem(
            db,
            timestamp=datetime.now(timezone.utc).isoformat(),
            severity=request.severity,
            title=request.title,
            details=request.details,
            source=request.source,
        )
    finally:
        db.close()

    return ProblemResponse(id=problem_id)


@router.get("/problems")
def get_problems(
    unresolved_only: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    x_api_key: str | None = Header(default=None),
):
    authorize(x_api_key)
    db = init_db()
    try:
        return {"problems": list_problems(db, unresolved_only=unresolved_only, limit=limit)}
    finally:
        db.close()
