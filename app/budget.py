"""Redis spend counters with soft/hard caps. Counters are authoritative for enforcement;
the `requests` table stays the source of truth for reporting."""

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.auth import Principal
from app.config import get_settings
from app.redis_client import get_redis

DAILY = "daily"
MONTHLY = "monthly"

OK = "ok"
SOFT = "soft"
HARD = "hard"

# Counters outlive their window so a late-arriving request cannot resurrect a stale key.
DAILY_TTL_S = 3 * 24 * 3600
MONTHLY_TTL_S = 40 * 24 * 3600


def _now() -> datetime:
    return datetime.now(UTC)


def window_id(period: str, now: datetime | None = None) -> str:
    now = now or _now()
    return now.strftime("%Y-%m-%d") if period == DAILY else now.strftime("%Y-%m")


def spend_key(tenant_id: int, period: str, now: datetime | None = None) -> str:
    return f"spend:{tenant_id}:{period}:{window_id(period, now)}"


def alert_key(tenant_id: int, kind: str, period: str, now: datetime | None = None) -> str:
    return f"alert:{tenant_id}:{kind}:{period}:{window_id(period, now)}"


@dataclass
class BudgetStatus:
    state: str
    daily_spend: float
    monthly_spend: float
    daily_budget: float
    monthly_budget: float
    breached_period: str | None = None

    @property
    def over_hard_cap(self) -> bool:
        return self.state == HARD

    @property
    def over_soft_cap(self) -> bool:
        return self.state == SOFT

    def as_error(self) -> dict:
        return {
            "error": {
                "message": (
                    f"{self.breached_period} budget exhausted: "
                    f"spent ${self.spend_for(self.breached_period):.4f} of "
                    f"${self.budget_for(self.breached_period):.2f}"
                ),
                "type": "budget_exceeded",
                "code": f"{self.breached_period}_hard_cap",
                "daily_spend_usd": round(self.daily_spend, 6),
                "daily_budget_usd": self.daily_budget,
                "monthly_spend_usd": round(self.monthly_spend, 6),
                "monthly_budget_usd": self.monthly_budget,
            }
        }

    def spend_for(self, period: str | None) -> float:
        return self.daily_spend if period == DAILY else self.monthly_spend

    def budget_for(self, period: str | None) -> float:
        return self.daily_budget if period == DAILY else self.monthly_budget


def get_spend(tenant_id: int) -> tuple[float, float]:
    client = get_redis()
    daily, monthly = client.mget(spend_key(tenant_id, DAILY), spend_key(tenant_id, MONTHLY))
    return float(daily or 0.0), float(monthly or 0.0)


def record_spend(tenant_id: int, cost_usd: float) -> None:
    if cost_usd <= 0:
        return
    client = get_redis()
    pipe = client.pipeline()
    for period, ttl in ((DAILY, DAILY_TTL_S), (MONTHLY, MONTHLY_TTL_S)):
        key = spend_key(tenant_id, period)
        pipe.incrbyfloat(key, cost_usd)
        pipe.expire(key, ttl)
    pipe.execute()


def check(principal: Principal) -> BudgetStatus:
    daily_spend, monthly_spend = get_spend(principal.tenant_id)
    ratio = get_settings().soft_cap_ratio

    periods = (
        (DAILY, daily_spend, principal.daily_budget_usd),
        (MONTHLY, monthly_spend, principal.monthly_budget_usd),
    )

    state, breached = OK, None
    for period, spend, budget in periods:
        if budget <= 0:
            continue
        if spend >= budget:
            state, breached = HARD, period
            break
        if spend >= budget * ratio and state == OK:
            state, breached = SOFT, period

    return BudgetStatus(
        state=state,
        daily_spend=daily_spend,
        monthly_spend=monthly_spend,
        daily_budget=principal.daily_budget_usd,
        monthly_budget=principal.monthly_budget_usd,
        breached_period=breached,
    )


def claim_alert(tenant_id: int, kind: str, period: str) -> bool:
    """Atomically claim the one alert allowed for this tenant/kind/window."""
    ttl = DAILY_TTL_S if period == DAILY else MONTHLY_TTL_S
    return bool(get_redis().set(alert_key(tenant_id, kind, period), "1", ex=ttl, nx=True))


def alert_payload(tenant_id: int, status: BudgetStatus) -> dict:
    period = status.breached_period
    return {
        "tenant_id": tenant_id,
        "kind": status.state,
        "period": period,
        "spend_usd": round(status.spend_for(period), 6),
        "budget_usd": status.budget_for(period),
        "text": (
            f"[autopilot] tenant {tenant_id} hit the {status.state} cap on its {period} budget: "
            f"${status.spend_for(period):.4f} of ${status.budget_for(period):.2f}"
        ),
    }


async def send_alert(tenant_id: int, status: BudgetStatus) -> bool:
    """Post to the configured webhook. Alerting must never break serving."""
    url = get_settings().alert_webhook_url
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=alert_payload(tenant_id, status))
        return True
    except Exception:
        return False
