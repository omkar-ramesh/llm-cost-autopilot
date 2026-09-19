import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlmodel import Session

from app import providers
from app.auth import Principal
from app.cache import get_cache
from app.config import get_settings
from app.db import get_engine
from app.models import Request
from app.pricing import cost_usd

PASSTHROUGH_TIER = "passthrough"


@dataclass
class GatewayResult:
    body: dict[str, Any]
    headers: dict[str, str]


def _as_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump()
    return json.loads(response.json())


def _headers(routed_model: str, cost: float, savings: float) -> dict[str, str]:
    return {
        "x-autopilot-model": routed_model,
        "x-autopilot-cost-usd": f"{cost:.8f}",
        "x-autopilot-savings-usd": f"{savings:.8f}",
    }


def _log_request(
    session: Session,
    principal: Principal,
    requested_model: str,
    routed_model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    status: str,
) -> Request:
    cost = cost_usd(routed_model, prompt_tokens, completion_tokens)
    baseline = cost_usd(requested_model, prompt_tokens, completion_tokens)
    row = Request(
        tenant_id=principal.tenant_id,
        key_id=principal.key_id,
        requested_model=requested_model,
        routed_model=routed_model,
        tier=PASSTHROUGH_TIER,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        baseline_cost_usd=baseline,
        latency_ms=latency_ms,
        cache_hit=False,
        status=status,
        complexity_score=0.0,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


async def handle_completion(
    payload: dict[str, Any], principal: Principal, session: Session
) -> GatewayResult:
    requested_model = payload.get("model") or get_settings().default_model
    routed_model = requested_model

    cache_key = json.dumps(payload, sort_keys=True, default=str)
    cached = await get_cache().get(cache_key)
    if cached is not None:
        row = _log_request(session, principal, requested_model, routed_model, 0, 0, 0, "ok")
        row.cache_hit = True
        session.add(row)
        session.commit()
        headers = _headers(routed_model, 0.0, row.baseline_cost_usd)
        return GatewayResult(body=cached, headers=headers)

    started = time.perf_counter()
    status = "ok"
    try:
        response = _as_dict(await providers.acompletion(**{**payload, "model": routed_model}))
    except Exception as exc:
        status = "error"
        _log_request(
            session, principal, requested_model, routed_model, 0, 0,
            int((time.perf_counter() - started) * 1000), status,
        )
        raise HTTPException(
            502, {"error": {"message": str(exc), "type": "upstream_error"}}
        ) from exc

    latency_ms = int((time.perf_counter() - started) * 1000)
    usage = response.get("usage") or {}
    row = _log_request(
        session,
        principal,
        requested_model,
        routed_model,
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        latency_ms,
        status,
    )
    savings = row.baseline_cost_usd - row.cost_usd
    return GatewayResult(body=response, headers=_headers(routed_model, row.cost_usd, savings))


async def stream_completion(
    payload: dict[str, Any], principal: Principal
) -> AsyncIterator[str]:
    requested_model = payload.get("model") or get_settings().default_model
    routed_model = requested_model
    started = time.perf_counter()
    prompt_tokens = completion_tokens = 0
    status = "ok"

    try:
        stream = await providers.acompletion(**{**payload, "model": routed_model, "stream": True})
        async for chunk in stream:
            data = _as_dict(chunk)
            usage = data.get("usage")
            if usage:
                prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                completion_tokens = usage.get("completion_tokens", completion_tokens)
            yield f"data: {json.dumps(data, default=str)}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as exc:
        status = "error"
        yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'upstream'}})}\n\n"
    finally:
        with Session(get_engine()) as session:
            _log_request(
                session,
                principal,
                requested_model,
                routed_model,
                prompt_tokens,
                completion_tokens,
                int((time.perf_counter() - started) * 1000),
                status,
            )
