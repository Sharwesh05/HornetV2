import os
import tempfile

os.environ.setdefault("HORNET_ADMIN_API_KEY", "test-key-0123456789abcdef")
os.environ.setdefault("HORNET_DATABASE_URL", "sqlite://")
os.environ.setdefault("HORNET_DISCOVERY_ALLOWED_PROJECTS", "shop-prod")
# Tests are hermetic: never use the developer's .env AI settings or key, and never call a real model.
# (Environment variables win over .env, so forcing these is enough.)
os.environ["HORNET_AI_PROVIDER"] = "template"
os.environ["HORNET_AI_SANITIZER_MODE"] = "off"
os.environ.pop("NVIDIA_API_KEY", None)
os.environ.pop("HORNET_NVIDIA_API_KEY", None)
os.environ.setdefault("HORNET_BUILD_DIR", tempfile.mkdtemp(prefix="hornet-builds-"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.immutability  # noqa: E402,F401
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402

API_KEY = os.environ["HORNET_ADMIN_API_KEY"]


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def db(session_factory):
    with session_factory() as s:
        yield s


@pytest.fixture
def client(session_factory):
    def _get_db():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app, headers={"X-API-Key": API_KEY}) as c:
        yield c
    app.dependency_overrides.clear()
