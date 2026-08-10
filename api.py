from fastapi import FastAPI

from web_api import router as api_router
from web_ui import router as ui_router

app = FastAPI(title="agent-lab API", version="0.1")


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(ui_router)
app.include_router(api_router)
