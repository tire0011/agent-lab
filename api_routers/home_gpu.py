"""Read routes for the home-GPU job table (#814, part 2c).

``GET /home-gpu/jobs`` lists ``home_gpu_jobs`` rows newest first. Only row
metadata is exposed - the answer itself stays in its file, so a row carries
just the answer's hash and path, never its text. The limit is clamped to
1-200 by :func:`modules.home_gpu.collect.list_jobs`.
"""

from fastapi import APIRouter, Depends, Header, Query

from models import HomeGpuJobEntry, HomeGpuJobsResponse
from modules.experiment_service import ExperimentService
from modules.home_gpu.collect import list_jobs

import api
from api import app, get_experiment_service

router = APIRouter(dependency_overrides_provider=app)

HOME_GPU_JOBS_DEFAULT_LIMIT = 20
HOME_GPU_JOBS_MAX_LIMIT = 200


@router.get("/home-gpu/jobs", response_model=HomeGpuJobsResponse)
def list_home_gpu_jobs(
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=HOME_GPU_JOBS_DEFAULT_LIMIT),
    service: ExperimentService = Depends(get_experiment_service),
) -> HomeGpuJobsResponse:
    """``home_gpu_jobs`` rows, newest first, answer hash and path only (#814)."""
    api.authorize(x_api_key)
    db_path = str(getattr(service, "db_path", api.DEFAULT_DB_PATH))
    rows = list_jobs(db_path, limit=limit)
    return HomeGpuJobsResponse(
        count=len(rows),
        jobs=[HomeGpuJobEntry(**row) for row in rows],
    )
