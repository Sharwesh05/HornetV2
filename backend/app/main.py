import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session, sessionmaker

import app.immutability as _guards  # noqa: F401  (registers the session guards)
from app.api.v1 import router
from app.config import get_settings

REAP_INTERVAL_S = 30
RETENTION_INTERVAL_S = 3600


async def _reaper(application: FastAPI) -> None:
    while True:
        await asyncio.sleep(REAP_INTERVAL_S)
        mgr = getattr(application.state, "manager", None)
        if mgr is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(mgr.reap)


def run_retention(days: int, factory: "sessionmaker[Session] | None" = None) -> dict[str, int]:
    """One automatic retention pass (HORNET_RETENTION_AUTO): the same audited purge as the CLI."""
    from app import retention
    from app.db import get_sessionmaker

    with (factory or get_sessionmaker())() as db:
        return retention.purge(db, days, actor="retention-auto")


async def _retention(days: int) -> None:
    while True:
        await asyncio.sleep(RETENTION_INTERVAL_S)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(run_retention, days)


@asynccontextmanager
async def lifespan(application: FastAPI):
    task = asyncio.create_task(_reaper(application))
    settings = get_settings()
    purger = asyncio.create_task(_retention(settings.retention_days)) if settings.retention_auto else None
    if settings.gateway_enabled:  # fail fast: a gateway without a working runtime would only hand out errors
        from app.db import get_sessionmaker
        from app.gateway.server import start_gateway
        from app.runtime.factory import ensure_manager

        db_factory = get_sessionmaker()
        mgr = await asyncio.to_thread(ensure_manager, application.state, settings, db_factory)
        application.state.gateway = await start_gateway(mgr, settings, db_factory)
    try:
        yield
    finally:
        task.cancel()
        if purger is not None:
            purger.cancel()
        gateway = getattr(application.state, "gateway", None)
        if gateway is not None:  # hang up on attackers first; their twins are destroyed with the connections
            await gateway.close()
        running = getattr(application.state, "manager", None)
        if running is not None:  # never leave attacker runtimes behind
            await asyncio.to_thread(running.shutdown)


def create_app() -> FastAPI:
    settings = get_settings()  # fail fast on unsafe configuration
    application = FastAPI(title="Hornet admin API", version="0.2.0", lifespan=lifespan)
    # only the dashboard's own origins; credentials are an explicit X-API-Key header, never cookies
    application.add_middleware(CORSMiddleware, allow_origins=settings.dashboard_origin_list(), allow_credentials=False,
                               allow_methods=["GET", "POST", "DELETE"], allow_headers=["X-API-Key", "Content-Type"])
    application.include_router(router)
    return application


app = create_app()
