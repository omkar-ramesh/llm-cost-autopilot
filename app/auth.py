import hashlib
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models import ApiKey


@dataclass
class Principal:
    tenant_id: int
    key_id: int
    daily_budget_usd: float
    monthly_budget_usd: float


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def authenticate(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, {"error": {"message": "missing bearer token", "type": "auth"}})

    raw_key = authorization.split(" ", 1)[1].strip()
    key = session.exec(select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key))).first()
    if key is None or not key.active:
        raise HTTPException(401, {"error": {"message": "invalid api key", "type": "auth"}})

    return Principal(
        tenant_id=key.tenant_id,
        key_id=key.id,
        daily_budget_usd=key.daily_budget_usd,
        monthly_budget_usd=key.monthly_budget_usd,
    )
