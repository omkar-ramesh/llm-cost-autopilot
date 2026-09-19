import os
import tempfile

import pytest

DB_FILE = os.path.join(tempfile.gettempdir(), "autopilot_test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE}"

from app import db  # noqa: E402
from app.db import DEV_KEY  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    from sqlmodel import SQLModel

    db._engine = None
    engine = db.get_engine()
    SQLModel.metadata.drop_all(engine)
    db.init_db()
    yield
    db._engine = None


@pytest.fixture(autouse=True)
def no_shadow_sampling(monkeypatch):
    """Shadow eval is random at 5%; tests that want it opt in explicitly."""
    from app import quality

    monkeypatch.setattr(quality, "should_sample", lambda: False)


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    """The cache is cross-cutting; tests that exercise it opt in via the `cache` fixture."""
    from app import cache, gateway

    monkeypatch.setattr(gateway, "get_cache", lambda: cache.NullCache())


@pytest.fixture
def cache(monkeypatch):
    from app import cache as cache_module
    from app import gateway

    live = cache_module.RedisCache(ttl_s=60)
    monkeypatch.setattr(gateway, "get_cache", lambda: live)
    return live


@pytest.fixture(autouse=True)
def fake_redis():
    import fakeredis

    from app import redis_client

    client = fakeredis.FakeRedis(decode_responses=True)
    redis_client.set_redis(client)
    yield client
    redis_client.set_redis(None)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {DEV_KEY}"})
        yield c


@pytest.fixture
def session():
    from sqlmodel import Session

    with Session(db.get_engine()) as s:
        yield s
