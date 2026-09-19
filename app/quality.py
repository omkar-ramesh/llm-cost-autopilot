"""Shadow eval: re-run a sampled routed-down request on the baseline model and have a
judge model score the cheap answer against it. Repeated failures escalate a prompt class."""

import json
import random
import re
from typing import Any

from sqlmodel import Session, select

from app import metrics, providers
from app.config import get_routing_config
from app.db import get_engine
from app.models import Eval, Request
from app.pricing import cost_usd
from app.redis_client import get_redis
from app.router import (
    CODE_PATTERNS,
    MATH_PATTERNS,
    MULTISTEP_PATTERNS,
    SCHEMA_PATTERNS,
    any_match,
    estimate_tokens,
    message_text,
)

ESCALATION_TTL_S = 24 * 3600
LONG_PROMPT_TOKENS = 400

JUDGE_SYSTEM = (
    "You grade whether a candidate answer is as useful as a reference answer for the same "
    "prompt. Reply with JSON only: {\"score\": <0.0-1.0>, \"reason\": \"<short>\"}. "
    "Score 1.0 means the candidate is equally or more useful; 0.0 means it is unusable."
)


def _fails_key(prompt_class: str) -> str:
    return f"quality:fails:{prompt_class}"


def _escalation_key(prompt_class: str) -> str:
    return f"quality:escalate:{prompt_class}"


def prompt_class(messages: list[dict[str, Any]], tools: list | None = None) -> str:
    """Coarse bucket used to escalate a whole family of prompts, not just one request."""
    text = " ".join(message_text(m) for m in messages)

    if tools:
        kind = "tools"
    elif any_match(CODE_PATTERNS, text):
        kind = "code"
    elif any_match(MATH_PATTERNS, text):
        kind = "math"
    elif any_match(SCHEMA_PATTERNS, text):
        kind = "schema"
    elif any_match(MULTISTEP_PATTERNS, text):
        kind = "multistep"
    else:
        kind = "plain"

    size = "long" if estimate_tokens(text) > LONG_PROMPT_TOKENS else "short"
    return f"{kind}:{size}"


def should_sample() -> bool:
    rate = get_routing_config()["shadow_eval"].get("sample_rate", 0.0)
    return random.random() < rate


def is_escalated(prompt_class: str) -> bool:
    try:
        return bool(get_redis().exists(_escalation_key(prompt_class)))
    except Exception:
        # Quality escalation is advisory; a Redis outage must not fail live traffic.
        return False


def record_outcome(prompt_class: str, passed: bool) -> bool:
    """Track consecutive failures. Returns True when this outcome escalated the class."""
    client = get_redis()
    if passed:
        client.delete(_fails_key(prompt_class))
        return False

    fails = client.incr(_fails_key(prompt_class))
    client.expire(_fails_key(prompt_class), ESCALATION_TTL_S)

    threshold = get_routing_config().get("escalate_after_fails", 3)
    if fails >= threshold:
        client.set(_escalation_key(prompt_class), "1", ex=ESCALATION_TTL_S)
        metrics.escalations_total.labels(prompt_class).inc()
        return True
    return False


def parse_judge_score(content: str) -> tuple[float, str]:
    """Judge output is untrusted text; fall back to the first number in the reply."""
    try:
        data = json.loads(re.sub(r"^```(json)?|```$", "", content.strip(), flags=re.M).strip())
        return max(0.0, min(float(data["score"]), 1.0)), str(data.get("reason", ""))[:500]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        match = re.search(r"\d*\.?\d+", content)
        if not match:
            return 0.0, "unparseable judge response"
        return max(0.0, min(float(match.group()), 1.0)), "parsed from unstructured judge reply"


def answer_text(response: dict[str, Any]) -> str:
    choices = response.get("choices") or [{}]
    return (choices[0].get("message") or {}).get("content") or ""


def _usage_cost(model: str, response: dict[str, Any]) -> float:
    usage = response.get("usage") or {}
    return cost_usd(model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))


async def run_shadow_eval(
    request_id: int,
    messages: list[dict[str, Any]],
    candidate_answer: str,
    baseline_model: str,
    prompt_class_name: str,
) -> Eval | None:
    """Run baseline + judge, persist the score, and escalate the class after N fails."""
    config = get_routing_config()["shadow_eval"]
    judge_model = config.get("judge", "gpt-4o")
    threshold = config.get("pass_threshold", 0.8)

    try:
        baseline = providers.as_dict(
            await providers.acompletion(model=baseline_model, messages=messages)
        )
        reference = answer_text(baseline)

        question = "\n".join(message_text(m) for m in messages if m.get("role") == "user")
        verdict = providers.as_dict(
            await providers.acompletion(
                model=judge_model,
                messages=[
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"PROMPT:\n{question}\n\n"
                            f"REFERENCE ANSWER:\n{reference}\n\n"
                            f"CANDIDATE ANSWER:\n{candidate_answer}"
                        ),
                    },
                ],
            )
        )
    except Exception:
        metrics.shadow_eval_errors_total.inc()
        return None

    score, reason = parse_judge_score(answer_text(verdict))
    passed = score >= threshold

    shadow_cost = _usage_cost(baseline_model, baseline) + _usage_cost(judge_model, verdict)
    metrics.shadow_eval_cost_usd_total.inc(shadow_cost)

    with Session(get_engine()) as session:
        row = Eval(
            request_id=request_id,
            judge_model=judge_model,
            score=score,
            passed=passed,
            reason=reason,
            prompt_class=prompt_class_name,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        tier = session.get(Request, request_id).tier

    metrics.evals_total.labels(tier, str(passed).lower()).inc()
    record_outcome(prompt_class_name, passed)
    return row


def pass_rate_by_tier(session: Session) -> dict[str, Any]:
    rows = session.exec(
        select(Request.tier, Eval.passed, Eval.score).join(Eval, Eval.request_id == Request.id)
    ).all()

    buckets: dict[str, dict[str, Any]] = {}
    for tier, passed, score in rows:
        bucket = buckets.setdefault(tier, {"evals": 0, "passed": 0, "score_total": 0.0})
        bucket["evals"] += 1
        bucket["passed"] += int(bool(passed))
        bucket["score_total"] += score

    tiers = {}
    for tier, bucket in buckets.items():
        n = bucket["evals"]
        tiers[tier] = {
            "evals": n,
            "passed": bucket["passed"],
            "pass_rate": round(bucket["passed"] / n, 4),
            "avg_score": round(bucket["score_total"] / n, 4),
        }

    total = sum(b["evals"] for b in buckets.values())
    total_passed = sum(b["passed"] for b in buckets.values())
    return {
        "tiers": tiers,
        "overall": {
            "evals": total,
            "passed": total_passed,
            "pass_rate": round(total_passed / total, 4) if total else None,
        },
        "pass_threshold": get_routing_config()["shadow_eval"].get("pass_threshold", 0.8),
    }


