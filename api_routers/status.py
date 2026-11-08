from datetime import datetime

from fastapi import APIRouter, Query
from models import ProblemsResponse, StatusResponse

from api import app
from models import Problem
import api
import chat as chat_module

router = APIRouter(dependency_overrides_provider=app)


@router.get("/status", response_model=StatusResponse)
def status() -> StatusResponse:
    return StatusResponse(**chat_module.build_status())


@router.get("/problems", response_model=ProblemsResponse)
def problems(
    limit: int = Query(default=20, ge=1, le=200),
    since: datetime | None = Query(default=None),
) -> ProblemsResponse:
    found: list[Problem] = api.list_problems(limit=limit, since=since)
    return ProblemsResponse(count=len(found), problems=found)
