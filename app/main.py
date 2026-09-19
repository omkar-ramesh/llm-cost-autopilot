from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi import Request as FastAPIRequest
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlmodel import Session

from app import gateway, metrics
from app.auth import Principal, authenticate
from app.db import get_session, init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="LLM Cost Autopilot", lifespan=lifespan)


@app.get("/metrics")
async def prometheus_metrics() -> Response:
    return Response(generate_latest(metrics.REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(
    request: FastAPIRequest,
    principal: Principal = Depends(authenticate),
    session: Session = Depends(get_session),
) -> Any:
    payload = await request.json()

    if payload.get("stream"):
        return StreamingResponse(
            gateway.stream_completion(payload, principal),
            media_type="text/event-stream",
            headers={"x-autopilot-model": payload.get("model", "")},
        )

    result = await gateway.handle_completion(payload, principal, session)
    return JSONResponse(content=result.body, headers=result.headers)
