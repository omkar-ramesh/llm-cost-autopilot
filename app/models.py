from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class Tenant(SQLModel, table=True):
    __tablename__ = "tenants"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    created_at: datetime = Field(default_factory=utcnow)


class ApiKey(SQLModel, table=True):
    __tablename__ = "api_keys"

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenants.id", index=True)
    key_hash: str = Field(index=True, unique=True)
    daily_budget_usd: float = 10.0
    monthly_budget_usd: float = 200.0
    active: bool = True


class BudgetEvent(SQLModel, table=True):
    __tablename__ = "budget_events"

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: int = Field(index=True)
    kind: str  # soft | hard
    period: str  # daily | monthly
    spend_usd: float
    budget_usd: float
    ts: datetime = Field(default_factory=utcnow, index=True)


class Eval(SQLModel, table=True):
    __tablename__ = "evals"

    id: int | None = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="requests.id", index=True)
    judge_model: str
    score: float
    passed: bool
    reason: str = ""
    prompt_class: str = Field(default="", index=True)
    ts: datetime = Field(default_factory=utcnow, index=True)


class Request(SQLModel, table=True):
    __tablename__ = "requests"

    id: int | None = Field(default=None, primary_key=True)
    tenant_id: int = Field(index=True)
    key_id: int = Field(index=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    requested_model: str
    routed_model: str
    tier: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    baseline_cost_usd: float = 0.0
    latency_ms: int = 0
    cache_hit: bool = False
    status: str = "ok"
    complexity_score: float = 0.0
