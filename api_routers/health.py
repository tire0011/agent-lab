from fastapi import APIRouter, Header
from models import HealthResponse

from api import app

router = APIRouter(dependency_overrides_provider=app)


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    checkout_revision = _read_git_revision()
    return HealthResponse(
        status="ok",
        version=VERSION,
        git_revision=_STARTUP_REVISION,
        checkout_revision=checkout_revision,
        stale=_revisions_differ(_STARTUP_REVISION, checkout_revision),
    )

@router.get("/secure-health")
def secure_health(x_api_key: str | None = Header(default=None)):
    authorize(x_api_key)
    return {"status": "ok", "auth": "ok"}
