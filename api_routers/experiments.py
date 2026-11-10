from fastapi import APIRouter, Depends, Header, Query
from models import (
    ExperimentAdoptionStatus,
    ExperimentDetail,
    ExperimentDrift,
    ExperimentReflection,
    ExperimentsResponse,
    LocalReplay,
    LocalReplaysResponse,
    PublicExperimentSummary,
    ReflectionsResponse,
)
from modules import experiment_reflection, local_shadow_replay
from modules.experiment_adoption_service import ExperimentAdoptionService
from modules.experiment_service import ExperimentService

from api import LIMIT_MIN, app, get_experiment_adoption_service, get_experiment_service
from fastapi import HTTPException
from modules import redaction
from modules.experiment_adoption_service import ExperimentAdoptionError
from modules.experiment_service import ExperimentNotCancellableError
from modules.experiment_service import ExperimentNotFoundError
from modules.github_work_tracking import GitHubWorkTrackingService
import api

router = APIRouter(dependency_overrides_provider=app)
adoption_router = APIRouter(dependency_overrides_provider=app)
public_router = APIRouter(dependency_overrides_provider=app)


EXPERIMENTS_DEFAULT_LIMIT = 20
EXPERIMENTS_MAX_LIMIT = 100
REFLECTIONS_DEFAULT_LIMIT = 20
REFLECTIONS_MAX_LIMIT = 100
LOCAL_REPLAYS_DEFAULT_LIMIT = 20
LOCAL_REPLAYS_MAX_LIMIT = 200


# Experiment and activity log endpoints: thin routes over the existing
# ExperimentService/run_store persistence; no new orchestration or SQLite access here.


@router.get("/experiments", response_model=ExperimentsResponse)
def list_experiments(
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=EXPERIMENTS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=EXPERIMENTS_MAX_LIMIT),
    service: ExperimentService = Depends(get_experiment_service),
) -> ExperimentsResponse:
    api.authorize(x_api_key)
    experiments = service.list_experiments(limit=limit)
    return ExperimentsResponse(
        count=len(experiments),
        experiments=[exp.to_dict() for exp in experiments],
    )


@router.get("/experiments/{experiment_id}", response_model=ExperimentDetail)
def get_experiment(
    experiment_id: str,
    x_api_key: str | None = Header(default=None),
    service: ExperimentService = Depends(get_experiment_service),
) -> ExperimentDetail:
    api.authorize(x_api_key)
    try:
        experiment = service.resolve_experiment(experiment_id)
    except ExperimentNotFoundError:
        raise HTTPException(status_code=404, detail="experiment not found")
    adoption_service = api._resolve_experiment_adoption_service()
    adoption = adoption_service.get(experiment.id)
    payload = experiment.to_dict()
    if experiment.test_exit_code not in (None, 0):
        payload["test_failure_tail"] = redaction.test_failure_tail(experiment.test_output)
    if adoption.status != "not_started":
        payload["adoption"] = adoption.to_dict()
    return ExperimentDetail(**payload)


@router.get("/experiments/{experiment_id}/drift", response_model=ExperimentDrift)
def get_experiment_drift(
    experiment_id: str,
    x_api_key: str | None = Header(default=None),
    service: ExperimentService = Depends(get_experiment_service),
) -> ExperimentDrift:
    api.authorize(x_api_key)
    try:
        experiment = service.resolve_experiment(experiment_id)
    except ExperimentNotFoundError:
        raise HTTPException(status_code=404, detail="experiment not found")
    tracking = GitHubWorkTrackingService(
        db_path=str(getattr(service, "db_path", api.DEFAULT_DB_PATH)),
        repo_root=str(getattr(service, "repo_root", "/agent")),
    )
    try:
        return ExperimentDrift(**tracking.branch_drift(experiment.id, experiment.worktree_path))
    finally:
        tracking.close()


def _reflection_db_path(service: ExperimentService) -> str:
    return str(getattr(service, "db_path", api.DEFAULT_DB_PATH))


def _reflection_payload(stored: dict) -> ExperimentReflection:
    payload = dict(stored)
    if payload.get("error"):
        payload["error"] = redaction.redact(str(payload["error"]))
    payload["suggestions"] = [
        {str(k): str(v) for k, v in suggestion.items()} for suggestion in payload["suggestions"]
    ]
    return ExperimentReflection(**payload)


@router.get("/experiments/{experiment_id}/reflection", response_model=ExperimentReflection)
def get_experiment_reflection(
    experiment_id: str,
    x_api_key: str | None = Header(default=None),
    service: ExperimentService = Depends(get_experiment_service),
) -> ExperimentReflection:
    """The stored post-experiment reflection (#630); 404 when there is none."""
    api.authorize(x_api_key)
    try:
        experiment = service.resolve_experiment(experiment_id)
    except ExperimentNotFoundError:
        raise HTTPException(status_code=404, detail="experiment not found")
    stored = experiment_reflection.get_reflection(experiment.id, db_path=_reflection_db_path(service))
    if stored is None:
        raise HTTPException(status_code=404, detail="no reflection stored for this experiment")
    return _reflection_payload(stored)


@router.get("/reflections", response_model=ReflectionsResponse)
def list_reflections(
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=REFLECTIONS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=REFLECTIONS_MAX_LIMIT),
    service: ExperimentService = Depends(get_experiment_service),
) -> ReflectionsResponse:
    """Stored post-experiment reflections, newest first (#630)."""
    api.authorize(x_api_key)
    stored = experiment_reflection.list_reflections(limit, db_path=_reflection_db_path(service))
    reflections = [_reflection_payload(item) for item in stored]
    return ReflectionsResponse(count=len(reflections), reflections=reflections)


@router.get("/local-replays", response_model=LocalReplaysResponse)
def list_local_replays(
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=LOCAL_REPLAYS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=LOCAL_REPLAYS_MAX_LIMIT),
    service: ExperimentService = Depends(get_experiment_service),
) -> LocalReplaysResponse:
    """Local shadow replays (#637), newest first, with the local pass rate per variant.

    ``variants`` aggregates every finished replay (not only the listed ones);
    ``common`` only the experiments scored under every variant.
    """
    api.authorize(x_api_key)
    db_path = _reflection_db_path(service)
    replays = []
    for item in local_shadow_replay.list_replays(limit, db_path=db_path):
        if item.get("error"):
            item["error"] = redaction.redact(str(item["error"]))
        replays.append(LocalReplay(**item))
    summary = local_shadow_replay.variant_summary(db_path=db_path)
    return LocalReplaysResponse(count=len(replays), replays=replays, **summary)


@router.post("/experiments/{experiment_id}/cancel", response_model=ExperimentDetail)
def cancel_experiment(
    experiment_id: str,
    x_api_key: str | None = Header(default=None),
    x_cancelled_by: str | None = Header(default=None),
    service: ExperimentService = Depends(get_experiment_service),
) -> ExperimentDetail:
    api.authorize(x_api_key)
    try:
        experiment = service.cancel_experiment(experiment_id, x_cancelled_by or "api")
    except ExperimentNotFoundError:
        raise HTTPException(status_code=404, detail="experiment not found")
    except ExperimentNotCancellableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ExperimentDetail(**experiment.to_dict())


@adoption_router.post("/experiments/{experiment_id}/adopt", response_model=ExperimentAdoptionStatus, status_code=202)
def adopt_experiment(
    experiment_id: str,
    x_api_key: str | None = Header(default=None),
    service: ExperimentService = Depends(get_experiment_service),
    adoption_service: ExperimentAdoptionService = Depends(get_experiment_adoption_service),
) -> ExperimentAdoptionStatus:
    api.authorize(x_api_key)
    try:
        experiment = service.resolve_experiment(experiment_id)
    except ExperimentNotFoundError:
        raise HTTPException(status_code=404, detail="experiment not found")
    try:
        adoption = adoption_service.start_adoption(experiment.id)
    except ExperimentAdoptionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return ExperimentAdoptionStatus(**adoption.to_dict())


@adoption_router.get("/experiments/{experiment_id}/adoption", response_model=ExperimentAdoptionStatus)
def get_experiment_adoption(
    experiment_id: str,
    x_api_key: str | None = Header(default=None),
    service: ExperimentService = Depends(get_experiment_service),
    adoption_service: ExperimentAdoptionService = Depends(get_experiment_adoption_service),
) -> ExperimentAdoptionStatus:
    api.authorize(x_api_key)
    try:
        experiment = service.resolve_experiment(experiment_id)
    except ExperimentNotFoundError:
        raise HTTPException(status_code=404, detail="experiment not found")
    # Chosen contract: never 404 just because adoption was not started yet.
    payload = adoption_service.get(experiment.id).to_dict()
    payload["previous_failures"] = adoption_service.failure_history(experiment.id)
    return ExperimentAdoptionStatus(**payload)


def _public_experiment_usage(experiment) -> dict[str, int | None] | None:
    fields = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")
    totals: dict[str, int | None] = {field: None for field in fields}
    for usage in (
        experiment.research_usage,
        experiment.selection_usage,
        experiment.reformat_retry_usage,
        experiment.policy_replan_usage,
        experiment.implementation_usage,
    ):
        if not isinstance(usage, dict):
            continue
        for field in fields:
            value = usage.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[field] = (totals[field] or 0) + int(value)
    return totals if any(value is not None for value in totals.values()) else None


def _public_experiment(experiment, mailbox_task_id: str | None) -> PublicExperimentSummary:
    # Strict allowlist: never derive this response from to_dict/model_dump.
    return PublicExperimentSummary(
        id=experiment.id,
        mailbox_task_id=mailbox_task_id,
        phase=api._public_phase(experiment.status),
        status=experiment.status,
        created_at=experiment.created_at,
        ended_at=experiment.ended_at,
        failure_reason=api._failure_reason(experiment),
        test_exit_code=experiment.test_exit_code,
        changed_files=api._changed_file_names(experiment.git_status),
        test_summary=api._test_summary(experiment),
        usage=_public_experiment_usage(experiment),
    )


@public_router.get("/experiments/{experiment_id}/public", response_model=PublicExperimentSummary)
def get_public_experiment(
    experiment_id: str,
    service: ExperimentService = Depends(get_experiment_service),
) -> PublicExperimentSummary:
    try:
        experiment = service.resolve_public_experiment(experiment_id)
    except ExperimentNotFoundError:
        raise HTTPException(status_code=404, detail="experiment not found")
    return _public_experiment(experiment, api._mailbox_task_id(service, experiment.id))
