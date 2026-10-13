"""Application-level guards:

* published/archived snapshots and everything they own are immutable;
* evidence, AI proposal, session-event and audit tables are append-only.

Registered on the Session class, so every session (API, tests, scripts) is covered.
Phase 7 should add matching PostgreSQL triggers as defence in depth.
"""
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.models import (
    AdminAuditEvent,
    AiSynthesis,
    EnvironmentSnapshot,
    ExecutionObservation,
    GatewayEvent,
    ServiceProfile,
    SessionEvent,
    SnapshotStatus,
    TwinImage,
    VirtualNode,
)

LOCKED = {SnapshotStatus.published.value, SnapshotStatus.archived.value}
APPEND_ONLY = (SessionEvent, AdminAuditEvent, ExecutionObservation, AiSynthesis, GatewayEvent)
SNAPSHOT_OWNED = (VirtualNode, ServiceProfile, TwinImage)


class ImmutableError(Exception):
    """Raised when code tries to modify locked or append-only data."""


def _committed_status(session: Session, snapshot_id) -> str | None:
    with session.no_autoflush:
        snap = session.get(EnvironmentSnapshot, snapshot_id)
    if snap is None:
        return None
    hist = inspect(snap).attrs.status.history
    return hist.deleted[0] if hist.deleted else snap.status


def _check(session: Session, flush_context, instances) -> None:
    for obj in list(session.dirty) + list(session.deleted):
        if isinstance(obj, APPEND_ONLY):
            if obj in session.deleted or session.is_modified(obj, include_collections=False):
                raise ImmutableError(f"{type(obj).__name__} is append-only")

    for obj in session.deleted:
        if isinstance(obj, EnvironmentSnapshot) and obj.status in LOCKED:
            raise ImmutableError("published snapshots cannot be deleted")

    for obj in session.dirty:
        if isinstance(obj, EnvironmentSnapshot):
            state = inspect(obj)
            old = state.attrs.status.history.deleted
            old_status = old[0] if old else obj.status
            if old_status not in LOCKED:
                continue
            changed = {
                a.key
                for a in state.mapper.column_attrs
                if state.attrs[a.key].history.has_changes()
            }
            allowed_archive = (
                changed == {"status"}
                and old_status == SnapshotStatus.published.value
                and obj.status == SnapshotStatus.archived.value
            )
            if changed and not allowed_archive:
                raise ImmutableError("published snapshots are immutable")

    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        if isinstance(obj, SNAPSHOT_OWNED):
            if _committed_status(session, obj.snapshot_id) in LOCKED:
                raise ImmutableError(
                    f"{type(obj).__name__} belongs to a published snapshot and is immutable"
                )


event.listen(Session, "before_flush", _check)
