import threading

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.orchestration.session_manager import SessionManager
from app.runtime.driver import RuntimeDriver


def build_driver(settings: Settings) -> RuntimeDriver:
    if settings.runtime_driver == "docker":
        from app.runtime.docker_driver import HardenedDockerDriver

        return HardenedDockerDriver()
    raise ValueError(f"unknown runtime driver {settings.runtime_driver!r}")


def build_manager(settings: Settings, db_factory: sessionmaker[Session]) -> SessionManager:
    return SessionManager(build_driver(settings), db_factory, settings.execution_policy(), settings.twin_base_image)


_lock = threading.Lock()


def ensure_manager(state, settings: Settings, db_factory: sessionmaker[Session]) -> SessionManager:
    """The one orchestrator of this process, shared by the admin API and the SSH gateway. Created on first use;
    on creation it removes orphan twins left by a previous crash (so only one process may manage a Docker host)."""
    mgr = getattr(state, "manager", None)
    if mgr is None:
        with _lock:
            mgr = getattr(state, "manager", None)
            if mgr is None:
                mgr = build_manager(settings, db_factory)
                mgr.reconcile()
                state.manager = mgr
    return mgr
