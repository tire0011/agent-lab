import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import chat as chat_module
from data_sources import list_problems, list_todos
from models import (
    ChatRequest,
    ChatResponse,
    HealthResponse,
    Problem,
    ProblemsResponse,
    StatusResponse,
    Todo,
    TodosResponse,
)

app = FastAPI()

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


@app.get("/todos", response_model=TodosResponse)
def todos() -> TodosResponse:
    found: list[Todo] = list_todos()
    return TodosResponse(count=len(found), todos=found)


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message must not be empty")

    reply, intent = chat_module.answer(message)
    return ChatResponse(reply=reply, intent=intent)
