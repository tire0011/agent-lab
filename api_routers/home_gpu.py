"""Authenticated read access to home-GPU job metadata."""

from contextlib import closing
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query

import api
from modules.run_store import init_db

router = APIRouter(dependency_overrides_provider=api.app)
HOME_GPU_JOBS_DEFAULT_LIMIT = 20
HOME_GPU_JOBS_MAX_LIMIT = 100


@router.get("/home-gpu/jobs")
def list_home_gpu_jobs(
    x_api_key: Annotated[str | None, Header()] = None,
    limit: Annotated[int, Query(ge=api.LIMIT_MIN, le=HOME_GPU_JOBS_MAX_LIMIT)] = HOME_GPU_JOBS_DEFAULT_LIMIT,
) -> dict[str, Any]:
    api.authorize(x_api_key)
    with closing(init_db(api.DEFAULT_DB_PATH)) as db:
        cursor = db.execute(
            "SELECT * FROM home_gpu_jobs ORDER BY submitted_at DESC, job_id DESC LIMIT ?",
            (limit,),
        )
        columns = [column[0] for column in cursor.description]
        jobs = [dict(zip(columns, row)) for row in cursor]
    return {"count": len(jobs), "jobs": jobs}
