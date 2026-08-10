from fastapi import FastAPI

from web_api import router

app = FastAPI(title="agent-lab API", version="0.1")


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(router)
