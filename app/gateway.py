import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlmodel import Session

from app import metrics, providers, router
from app.auth import Principal
from app.cache import get_cache
from app.config import get_routing_config, get_settings
from app.db import get_engine
from app.models import Request
from app.pricing import cost_usd

MODEL_HEADER = "x-autopilot-model"
MODE_HEADER = "x-autopilot-mode"

RETRYABLE_STATUS = {408, 409, 429}


@dataclass
class GatewayResult:
    body: dict[str, Any]
    headers: dict[str, str]


def is_retryable(exc: Exception) -> bool:
    """Retry on the conditions listed under `fallback_on` in routing.yaml."""
    # YAML parses `429` as an int, so normalise every trigger to a string.
    triggers = {str(t).lower() for t in get_routing_config().get("fallback_on", [])}
    name = type(exc).__name__.lower()

    if "timeout" in triggers and "timeout" in name:
        return True

    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        return False
    if "429" in triggers and status in RETRYABLE_STATUS:
        return True
    return "5xx" in triggers and status >= 500


def _as_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump()
    return json.loads(response.json())


def _headers(decision: router.RoutingDecision, routed_model: str, cost: float, savings: float):
    return {
        MODEL_HEADER: routed_model,
        "x-autopilot-cost-usd": f"{cost:.8f}",
        "x-autopilot-savings-usd": f"{savings:.8f}",
        "x-autopilot-tier": decision.tier,
        "x-autopilot-complexity": f"{decision.score:.4f}",
    }


def _log_request(
    session: Session,
    principal: Principal,
    requested_model: str,
    routed_model: str,
    decision: router.RoutingDecision,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    status: str,
    cache_hit: bool = False,
) -> Request:
    cost = cost_usd(routed_model, prompt_tokens, completion_tokens)
    baseline = cost_usd(requested_model, prompt_tokens, completion_tokens)
    row = Request(
        tenant_id=principal.tenant_id,
        key_id=principal.key_id,
        requested_model=requested_model,
        routed_model=routed_model,
        tier=decision.tier,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        baseline_cost_usd=baseline,
        latency_ms=latency_ms,
        cache_hit=cache_hit,
        status=status,
        complexity_score=decision.score,
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    metrics.record_request(
        tenant_id=row.tenant_id,
        requested_model=requested_model,
        routed_model=routed_model,
        tier=decision.tier,
        status=status,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=row.cost_usd,
        baseline_cost_usd=row.baseline_cost_usd,
        latency_ms=latency_ms,
        cache_hit=cache_hit,
    )
    return row


def plan(payload: dict[str, Any], headers: dict[str, str]) -> tuple[str, router.RoutingDecision]:
    lowered = {k.lower(): v for k, v in headers.items()}
    requested_model = payload.get("model") or get_settings().default_model
    decision = router.route(
        payload.get("messages", []),
        mode=lowered.get(MODE_HEADER),
        model_override=lowered.get(MODEL_HEADER),
        tools=payload.get("tools"),
    )
    return requested_model, decision


async def _call_with_fallback(payload: dict[str, Any], decision: router.RoutingDecision):
    """Try each model in the chain, moving on when the error is in `fallback_on`."""
    last_exc: Exception | None = None
    for model in decision.models:
        try:
            return model, await providers.acompletion(**{**payload, "model": model})
        except Exception as exc:
            last_exc = exc
            if not is_retryable(exc):
                raise
            metrics.fallbacks_total.labels(model).inc()
    raise last_exc  # type: ignore[misc]


async def handle_completion(
    payload: dict[str, Any],
    principal: Principal,
    session: Session,
    headers: dict[str, str] | None = None,
) -> GatewayResult:
    requested_model, decision = plan(payload, headers or {})

    cache_key = json.dumps(payload, sort_keys=True, default=str)
    cached = await get_cache().get(cache_key)
    if cached is not None:
        row = _log_request(
            session, principal, requested_model, decision.primary, decision,
            0, 0, 0, "ok", cache_hit=True,
        )
        return GatewayResult(
            body=cached,
            headers=_headers(decision, decision.primary, 0.0, row.baseline_cost_usd),
        )

    started = time.perf_counter()
    try:
        routed_model, raw = await _call_with_fallback(payload, decision)
        response = _as_dict(raw)
    except Exception as exc:
        _log_request(
            session, principal, requested_model, decision.primary, decision,
            0, 0, int((time.perf_counter() - started) * 1000), "error",
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
        decision,
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        latency_ms,
        "ok",
    )
    savings = row.baseline_cost_usd - row.cost_usd
    return GatewayResult(
        body=response, headers=_headers(decision, routed_model, row.cost_usd, savings)
    )


async def stream_completion(
    payload: dict[str, Any],
    principal: Principal,
    headers: dict[str, str] | None = None,
) -> AsyncIterator[str]:
    requested_model, decision = plan(payload, headers or {})
    started = time.perf_counter()
    prompt_tokens = completion_tokens = 0
    status = "ok"
    routed_model = decision.primary

    try:
        routed_model, stream = await _call_with_fallback({**payload, "stream": True}, decision)
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
                decision,
                prompt_tokens,
                completion_tokens,
                int((time.perf_counter() - started) * 1000),
                status,
            )
