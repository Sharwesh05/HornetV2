import os
import tempfile
from pathlib import Path

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


def _neutralise_dev_env_file() -> None:
    """The developer's .env is found from any directory, so it would leak into tests (e.g. an enabled SSH gateway
    makes every API test need Docker). Environment variables win over .env, so every setting the .env defines is
    pinned to its code default, unless the test run already set it explicitly."""
    from dotenv import dotenv_values  # a dependency of pydantic-settings

    from app.config import ENV_FILES, Settings

    for env_file in ENV_FILES:
        for key in dotenv_values(env_file) if env_file.is_file() else {}:
            if key in os.environ:
                continue
            if key in ("NVIDIA_API_KEY", "HORNET_NVIDIA_API_KEY"):
                os.environ[key] = ""
                continue
            field = Settings.model_fields.get(key.removeprefix("HORNET_").lower()) if key.startswith("HORNET_") else None
            if field is not None and not field.is_required() and field.default is not None:
                os.environ[key] = str(field.default)


_neutralise_dev_env_file()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.immutability  # noqa: E402,F401
from app.db import Base, get_db, make_engine  # noqa: E402
from app.main import app  # noqa: E402

API_KEY = os.environ["HORNET_ADMIN_API_KEY"]


def file_session_factory(directory) -> sessionmaker:
    """A SQLite *file* database through the app's own make_engine, so every thread gets its own connection, as in
    production. (One shared in-memory connection, e.g. StaticPool, is not thread-safe: the gateway and the session
    manager use it from several threads at once, which crashed the interpreter on Linux.)"""
    engine = make_engine(f"sqlite:///{(Path(directory) / 'hornet-test.db').as_posix()}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session_factory(tmp_path):
    factory = file_session_factory(tmp_path)
    yield factory
    factory.kw["bind"].dispose()


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
