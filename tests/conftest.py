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
