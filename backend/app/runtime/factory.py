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


def build_synthesizer(settings: Settings, db_factory: sessionmaker[Session]):
    """The Phase 5A AI synthesis worker, or None when HORNET_AI_SYNTH_ENABLED is off (the default)."""
    if not settings.ai_synth_enabled:
        return None
    from app.ai.provider import TemplateProvider
    from app.ai.synthesis import SynthesisWorker

    if settings.ai_provider == "nim":
        from app.ai.nim_provider import NimProvider

        # its own client: a fast model, a short timeout and no retries (a late answer is a useless answer)
        provider = NimProvider(settings.nvidia_api_key.get_secret_value(), settings.nim_synth_model,
                               settings.nim_base_url, settings.ai_synth_timeout_s, max_tokens=1024, max_retries=0,
                               thinking=False, guided_json=False)
    else:
        provider = TemplateProvider()  # offline: every gap is recorded as "fallback: no model provider configured"
    return SynthesisWorker(provider, db_factory, timeout_s=settings.ai_synth_timeout_s,
                           max_per_session=settings.ai_synth_max_per_session)


def build_manager(settings: Settings, db_factory: sessionmaker[Session]) -> SessionManager:
    return SessionManager(build_driver(settings), db_factory, settings.execution_policy(), settings.twin_base_image,
                          store_raw_commands=settings.store_raw_commands,
                          synthesizer=build_synthesizer(settings, db_factory))


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
