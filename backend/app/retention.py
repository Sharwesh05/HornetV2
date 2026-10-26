"""Data retention: the ONE path allowed to delete recorded attacker data.

Evidence, AI and event tables are append-only (app/immutability.py guards every ORM flush). Retention still has
to remove old sessions, so this module deletes with bulk SQL statements, which do not go through a flush, and
writes a `retention.purge` audit event with the counts. On PostgreSQL the database triggers (migration 0005)
enforce the same rules and admit this purge only through a transaction-local flag. Live sessions are never purged, nor is anything
belonging to environment snapshots.
"""
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Delete, delete, or_, select, text
from sqlalchemy.orm import Session

from app import services
from app.models import (
    AiSynthesis,
    ExecutionObservation,
    ExecutionRequest,
    GatewayEvent,
    HoneypotSession,
    SessionEvent,
    SessionOverlay,
    SessionStatus,
)

ENDED = (SessionStatus.destroyed.value, SessionStatus.failed.value, SessionStatus.archived.value)


def purge(db: Session, older_than_days: int, actor: str, now: datetime | None = None) -> dict[str, int]:
    """Delete ended sessions (and everything recorded for them) that ended more than `older_than_days` ago, plus
    gateway events older than that which belong to no session. Returns the number of rows deleted per table."""
    if older_than_days < 1:
        raise ValueError("older_than_days must be at least 1")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=older_than_days)
    sessions = select(HoneypotSession.id).where(
        HoneypotSession.status.in_(ENDED),
        or_(HoneypotSession.ended_at < cutoff, HoneypotSession.ended_at.is_(None) & (HoneypotSession.started_at < cutoff)),
    )
    session_ids = list(db.scalars(sessions))
    requests = select(ExecutionRequest.id).where(ExecutionRequest.session_id.in_(session_ids))

    counts: dict[str, int] = {}
    if db.get_bind().dialect.name == "postgresql":
        # the database triggers (migration 0005) let DELETEs through only with this flag, for this transaction only
        db.execute(text("SET LOCAL hornet.retention_purge = 'on'"))

    def run(name: str, stmt: Delete) -> None:
        result = cast(CursorResult[Any], db.execute(stmt.execution_options(synchronize_session=False)))
        counts[name] = result.rowcount or 0

    # children before parents, so foreign keys hold on PostgreSQL
    run("ai_synthesis", delete(AiSynthesis).where(AiSynthesis.request_id.in_(requests)))
    run("execution_observation", delete(ExecutionObservation).where(ExecutionObservation.request_id.in_(requests)))
    run("execution_request", delete(ExecutionRequest).where(ExecutionRequest.session_id.in_(session_ids)))
    run("session_event", delete(SessionEvent).where(SessionEvent.session_id.in_(session_ids)))
    run("session_overlay", delete(SessionOverlay).where(SessionOverlay.session_id.in_(session_ids)))
    run("gateway_event", delete(GatewayEvent).where(or_(
        GatewayEvent.session_id.in_(session_ids),
        GatewayEvent.session_id.is_(None) & (GatewayEvent.occurred_at < cutoff),
    )))
    run("honeypot_session", delete(HoneypotSession).where(HoneypotSession.id.in_(session_ids)))
    services.audit(db, actor, "retention.purge", None, older_than_days=older_than_days, cutoff=cutoff.isoformat(),
                   **counts)
    db.commit()
    return counts
