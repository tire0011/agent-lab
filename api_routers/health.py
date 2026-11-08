from fastapi import APIRouter, Header
from models import HealthResponse

from api import app
from nexus_version import VERSION
import api

router = APIRouter(dependency_overrides_provider=app)


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    checkout_revision = api._read_git_revision()
    return HealthResponse(
        status="ok",
        version=VERSION,
        git_revision=api._STARTUP_REVISION,
        checkout_revision=checkout_revision,
        stale=api._revisions_differ(api._STARTUP_REVISION, checkout_revision),
    )

@router.get("/secure-health")
def secure_health(x_api_key: str | None = Header(default=None)):
    api.authorize(x_api_key)
    return {"status": "ok", "auth": "ok"}
