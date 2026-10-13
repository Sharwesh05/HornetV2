"""Data model (README section 6). Snapshot-owned rows are immutable once published,
and event/audit tables are append-only; both are enforced in app/immutability.py."""
import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime as SADateTime
from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, JsonType


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class SnapshotStatus(enum.StrEnum):
    draft = "draft"
    validated = "validated"
    published = "published"
    rejected = "rejected"
    archived = "archived"  # superseded by a newer published snapshot


class NodeType(enum.StrEnum):
    directory = "directory"
    file = "file"
    service = "service"
    user = "user"
    database = "database"
    endpoint = "endpoint"


class SessionStatus(enum.StrEnum):
    requested = "requested"
    provisioning = "provisioning"
    ready = "ready"
    active = "active"
    quiescing = "quiescing"
    archived = "archived"
    destroyed = "destroyed"
    failed = "failed"


def DateTime() -> SADateTime:  # noqa: N802  (timezone-aware everywhere)
    return SADateTime(timezone=True)


class EnvironmentSnapshot(Base):
    __tablename__ = "environment_snapshot"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    status: Mapped[str] = mapped_column(String(16), default=SnapshotStatus.draft.value, index=True)
    profile_hash: Mapped[str | None] = mapped_column(String(128))
    validation_report: Mapped[dict | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=_now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime())

    nodes: Mapped[list["VirtualNode"]] = relationship(back_populates="snapshot")


class VirtualNode(Base):
    __tablename__ = "virtual_node"
    __table_args__ = (UniqueConstraint("snapshot_id", "virtual_path"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environment_snapshot.id"), index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("virtual_node.id"), index=True)
    virtual_path: Mapped[str] = mapped_column(String(1024))
    node_type: Mapped[str] = mapped_column(String(16))
    permissions: Mapped[str | None] = mapped_column(String(16))
    content_ref: Mapped[str | None] = mapped_column(String(256))
    sensitivity_class: Mapped[str] = mapped_column(String(24), default="public")
    meta: Mapped[dict] = mapped_column("metadata", JsonType, default=dict)

    snapshot: Mapped[EnvironmentSnapshot] = relationship(back_populates="nodes")


class ServiceProfile(Base):
    __tablename__ = "service_profile"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environment_snapshot.id"), index=True
    )
    service_type: Mapped[str] = mapped_column(String(64))
    behavior_config: Mapped[dict] = mapped_column(JsonType, default=dict)


class TwinImage(Base):
    __tablename__ = "twin_image"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environment_snapshot.id"), index=True
    )
    image_digest: Mapped[str] = mapped_column(String(128))
    runtime_type: Mapped[str] = mapped_column(String(32))


class HoneypotSession(Base):
    __tablename__ = "honeypot_session"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environment_snapshot.id"), index=True
    )
    runtime_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default=SessionStatus.requested.value)
    source_ip: Mapped[str | None] = mapped_column(String(64))
    ai_policy_version: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(), default=_now, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime())
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime())
    end_reason: Mapped[str | None] = mapped_column(String(64))


class ExecutionRequest(Base):
    __tablename__ = "execution_request"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("honeypot_session.id"), index=True)
    operation: Mapped[str] = mapped_column(String(32))
    input_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=_now)


class ExecutionObservation(Base):
    """Raw execution evidence. Never produced by the AI."""

    __tablename__ = "execution_observation"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("execution_request.id"), index=True)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    result_metadata: Mapped[dict] = mapped_column(JsonType, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(), default=_now)


class AiSynthesis(Base):
    """AI proposals, stored separately from observations."""

    __tablename__ = "ai_synthesis"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("execution_request.id"), index=True)
    response_mode: Mapped[str] = mapped_column(String(32))
    validation_status: Mapped[str] = mapped_column(String(24))
    response_metadata: Mapped[dict] = mapped_column(JsonType, default=dict)


class SessionEvent(Base):
    __tablename__ = "session_event"
    __table_args__ = (Index("ix_session_event_session_time", "session_id", "occurred_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("honeypot_session.id"))
    node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("virtual_node.id"))
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    meta: Mapped[dict] = mapped_column("metadata", JsonType, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(), default=_now)


class GatewayEvent(Base):
    """Events at the attacker-facing door: connections, login attempts, refused requests. They happen before a
    twin exists, so they cannot be session events; `session_id` is filled in once a login leads to a session."""

    __tablename__ = "gateway_event"
    __table_args__ = (Index("ix_gateway_event_conn_time", "connection_id", "occurred_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("honeypot_session.id"), index=True)
    source_ip: Mapped[str | None] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    meta: Mapped[dict] = mapped_column("metadata", JsonType, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(), default=_now, index=True)


class SessionOverlay(Base):
    __tablename__ = "session_overlay"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("honeypot_session.id"), index=True)
    virtual_path: Mapped[str] = mapped_column(String(1024))
    operation: Mapped[str] = mapped_column(String(16))
    meta: Mapped[dict] = mapped_column("metadata", JsonType, default=dict)


class AdminAuditEvent(Base):
    """Administrator actions, kept apart from attacker session events."""

    __tablename__ = "admin_audit_event"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[dict] = mapped_column(JsonType, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(), default=_now, index=True)
