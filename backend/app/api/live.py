"""Live event feed for the admin dashboard: WebSocket /api/v1/ws/events.

Protocol:
  1. the client connects and, within AUTH_TIMEOUT_S, sends {"api_key": "<admin key>"} (browsers cannot set an
     X-API-Key header on a WebSocket, so the key travels in the first message, never in the URL);
  2. the server answers {"type": "ready"} or closes with 4401;
  3. the server then pushes every new session event and gateway event as
     {"type": "session_event" | "gateway_event", "id", "session_id", "event_type", ..., "metadata", "occurred_at"}.

New events are found by polling the database every POLL_S seconds, so the feed works with any number of API or
gateway processes writing to the same database. Connections from an Origin outside HORNET_DASHBOARD_ORIGINS are
refused (4403). Event metadata contains attacker-supplied text: the dashboard renders it as plain text.
"""
import asyncio
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import services
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.models import GatewayEvent, SessionEvent

POLL_S = 1.0
AUTH_TIMEOUT_S = 5.0
BACKLOG = timedelta(seconds=60)  # events this old at connect time are still sent (catches the last few seconds)
WINDOW = timedelta(minutes=10)  # how far back each poll looks; ids already sent are skipped
BATCH = 500
CLOSE_UNAUTHORIZED, CLOSE_ORIGIN = 4401, 4403

router = APIRouter()


def get_session_factory() -> sessionmaker[Session]:
    return get_sessionmaker()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _poll(factory: sessionmaker[Session], since: datetime, seen: dict[uuid.UUID, datetime]) -> list[dict]:
    out: list[dict] = []
    with factory() as db:
        for ev in db.scalars(select(SessionEvent).where(SessionEvent.occurred_at >= since)
                             .order_by(SessionEvent.occurred_at).limit(BATCH)):
            if ev.id not in seen:
                seen[ev.id] = _aware(ev.occurred_at)
                out.append({"type": "session_event", "id": ev.id, "session_id": ev.session_id, "node_id": ev.node_id,
                            "event_type": ev.event_type, "metadata": ev.meta, "occurred_at": _aware(ev.occurred_at)})
        for gev in db.scalars(select(GatewayEvent).where(GatewayEvent.occurred_at >= since)
                              .order_by(GatewayEvent.occurred_at).limit(BATCH)):
            if gev.id not in seen:
                seen[gev.id] = _aware(gev.occurred_at)
                out.append({"type": "gateway_event", "id": gev.id, "session_id": gev.session_id,
                            "connection_id": gev.connection_id, "source_ip": gev.source_ip,
                            "event_type": gev.event_type, "metadata": gev.meta,
                            "occurred_at": _aware(gev.occurred_at)})
    out.sort(key=lambda e: e["occurred_at"])
    return out


def _audit(factory: sessionmaker[Session], origin: str | None) -> None:
    with factory() as db:
        services.audit(db, "admin-api-key", "live.connect", None, origin=origin)
        db.commit()


@router.websocket("/ws/events")
async def live_events(ws: WebSocket, settings: Settings = Depends(get_settings),
                      factory: sessionmaker[Session] = Depends(get_session_factory)) -> None:
    origin = ws.headers.get("origin")
    if origin is not None and origin not in settings.dashboard_origin_list():
        await ws.close(code=CLOSE_ORIGIN)  # a page on another site must not reach the feed (cross-site WebSocket)
        return
    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_S)
        key = json.loads(first).get("api_key", "") if first.startswith("{") else ""
    except (TimeoutError, WebSocketDisconnect, ValueError, AttributeError):
        key = ""
    if not isinstance(key, str) or not key or not secrets.compare_digest(key.encode(), settings.admin_api_key.encode()):
        await ws.close(code=CLOSE_UNAUTHORIZED)
        return
    await asyncio.to_thread(_audit, factory, origin)
    await ws.send_json({"type": "ready", "poll_s": POLL_S})

    connected = datetime.now(UTC)
    seen: dict[uuid.UUID, datetime] = {}
    try:
        while True:
            since = max(connected - BACKLOG, datetime.now(UTC) - WINDOW)
            for k in [k for k, t in seen.items() if t < since]:
                del seen[k]
            for event in await asyncio.to_thread(_poll, factory, since, seen):
                await ws.send_text(json.dumps(event, default=str))
            await asyncio.sleep(POLL_S)
    except (WebSocketDisconnect, RuntimeError):
        return
