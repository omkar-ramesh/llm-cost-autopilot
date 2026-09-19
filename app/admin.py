"""Admin API for tenants, keys and budgets. Guarded by a static admin token."""

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app import budget
from app.auth import hash_key
from app.config import get_settings
from app.db import get_session
from app.models import ApiKey, Tenant

router = APIRouter(prefix="/v1/admin", tags=["admin"])

KEY_PREFIX = "sk-ap-"


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    expected = get_settings().admin_token
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(401, {"error": {"message": "invalid admin token", "type": "auth"}})


class TenantIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class KeyIn(BaseModel):
    tenant_id: int
    daily_budget_usd: float = Field(default=10.0, ge=0)
    monthly_budget_usd: float = Field(default=200.0, ge=0)


class BudgetIn(BaseModel):
    daily_budget_usd: float | None = Field(default=None, ge=0)
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    active: bool | None = None


def _get_key(session: Session, key_id: int) -> ApiKey:
    key = session.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(404, {"error": {"message": "key not found", "type": "not_found"}})
    return key


@router.post("/tenants", status_code=201, dependencies=[Depends(require_admin)])
def create_tenant(body: TenantIn, session: Session = Depends(get_session)) -> dict:
    tenant = Tenant(name=body.name)
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    return {"id": tenant.id, "name": tenant.name, "created_at": tenant.created_at.isoformat()}


@router.get("/tenants", dependencies=[Depends(require_admin)])
def list_tenants(session: Session = Depends(get_session)) -> dict:
    tenants = session.exec(select(Tenant)).all()
    return {"tenants": [{"id": t.id, "name": t.name} for t in tenants]}


@router.post("/keys", status_code=201, dependencies=[Depends(require_admin)])
def create_key(body: KeyIn, session: Session = Depends(get_session)) -> dict:
    if session.get(Tenant, body.tenant_id) is None:
        raise HTTPException(404, {"error": {"message": "tenant not found", "type": "not_found"}})

    raw_key = KEY_PREFIX + secrets.token_urlsafe(32)
    key = ApiKey(
        tenant_id=body.tenant_id,
        key_hash=hash_key(raw_key),
        daily_budget_usd=body.daily_budget_usd,
        monthly_budget_usd=body.monthly_budget_usd,
    )
    session.add(key)
    session.commit()
    session.refresh(key)

    # The raw key is shown once; only its hash is stored.
    return {
        "id": key.id,
        "tenant_id": key.tenant_id,
        "api_key": raw_key,
        "daily_budget_usd": key.daily_budget_usd,
        "monthly_budget_usd": key.monthly_budget_usd,
    }


@router.patch("/keys/{key_id}/budget", dependencies=[Depends(require_admin)])
def update_budget(
    key_id: int, body: BudgetIn, session: Session = Depends(get_session)
) -> dict:
    key = _get_key(session, key_id)

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(key, field, value)

    session.add(key)
    session.commit()
    session.refresh(key)
    return {
        "id": key.id,
        "tenant_id": key.tenant_id,
        "daily_budget_usd": key.daily_budget_usd,
        "monthly_budget_usd": key.monthly_budget_usd,
        "active": key.active,
    }


@router.get("/tenants/{tenant_id}/spend", dependencies=[Depends(require_admin)])
def tenant_spend(tenant_id: int, session: Session = Depends(get_session)) -> dict:
    if session.get(Tenant, tenant_id) is None:
        raise HTTPException(404, {"error": {"message": "tenant not found", "type": "not_found"}})

    daily, monthly = budget.get_spend(tenant_id)
    keys = session.exec(select(ApiKey).where(ApiKey.tenant_id == tenant_id)).all()
    return {
        "tenant_id": tenant_id,
        "daily_spend_usd": round(daily, 6),
        "monthly_spend_usd": round(monthly, 6),
        "keys": [
            {
                "id": k.id,
                "active": k.active,
                "daily_budget_usd": k.daily_budget_usd,
                "monthly_budget_usd": k.monthly_budget_usd,
            }
            for k in keys
        ],
    }
