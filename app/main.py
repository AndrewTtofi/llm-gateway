"""Gateway entrypoint. Phase 0: health, model listing, config reload."""

from __future__ import annotations

import secrets

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app import config

app = FastAPI(title="LLM Gateway", version="0.0.1")


@app.exception_handler(HTTPException)
async def openai_error(_: Request, exc: HTTPException) -> JSONResponse:
    """Return errors in OpenAI's shape so SDK clients parse them correctly."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": str(exc.detail),
                "type": "invalid_request_error",
                "code": exc.status_code,
            }
        },
        headers=exc.headers,
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models() -> dict[str, object]:
    reg = config.registry
    data = [{"id": name, "object": "model", "chain": a.chain} for name, a in reg.aliases.items()]
    return {"object": "list", "data": data}


@app.post("/admin/reload")
async def reload(authorization: str = Header(default="")) -> dict[str, object]:
    key = config.settings.gateway_admin_key
    if not key or not secrets.compare_digest(authorization, f"Bearer {key}"):
        raise HTTPException(status_code=401, detail="unauthorized")
    reg = config.reload_registry()
    return {"reloaded": True, "aliases": list(reg.aliases)}


# Phase 1: POST /v1/chat/completions goes here.
