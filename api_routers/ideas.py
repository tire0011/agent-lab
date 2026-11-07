from fastapi import APIRouter, Header
from models import Idea, IdeaCreateRequest, IdeasResponse

from api import app

router = APIRouter(dependency_overrides_provider=app)


@router.get("/ideas", response_model=IdeasResponse)
def ideas() -> IdeasResponse:
    found: list[Idea] = list_ideas()
    return IdeasResponse(count=len(found), ideas=found)


@router.post("/ideas", response_model=Idea, status_code=201)
def create_idea(
    request: IdeaCreateRequest,
    x_api_key: str | None = Header(default=None),
) -> Idea:
    authorize(x_api_key)

    try:
        return add_idea(request.text)
    except InvalidIdeaTextError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
