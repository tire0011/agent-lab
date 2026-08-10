import os
from fastapi import FastAPI, Header, HTTPException

app = FastAPI()

API_KEY = os.environ["AGENT_API_KEY"]

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