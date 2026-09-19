from collections.abc import Iterator

from sqlmodel import Session, SQLModel, create_engine, select

from app.config import get_settings
from app.models import ApiKey, Tenant

_engine = None

DEV_KEY = "sk-autopilot-dev"


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session


def init_db() -> None:
    SQLModel.metadata.create_all(get_engine())
    seed_dev_tenant()


def seed_dev_tenant() -> None:
    from app.auth import hash_key

    with Session(get_engine()) as session:
        key_hash = hash_key(DEV_KEY)
        if session.exec(select(ApiKey).where(ApiKey.key_hash == key_hash)).first():
            return
        tenant = Tenant(name="dev")
        session.add(tenant)
        session.commit()
        session.refresh(tenant)
        session.add(ApiKey(tenant_id=tenant.id, key_hash=key_hash))
        session.commit()
