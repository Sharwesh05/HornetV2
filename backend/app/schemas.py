import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class NodeIn(BaseModel):
    path: str
    parent_path: str | None = None
    node_type: Literal["directory", "file", "service", "user", "database", "endpoint"]


class BuildRequest(BaseModel):
    source: Literal["seed", "docker"] = Field(
        default="seed", description="'docker' discovers an allowlisted compose project and builds a twin image"
    )
    project: str | None = Field(default=None, description="Compose project to observe (source=docker)")
    nodes: list[NodeIn] | None = Field(default=None, description="Optional hand-written tree (source=seed)")


class DiscoveryPreviewIn(BaseModel):
    project: str


class SnapshotOut(ORM):
    id: uuid.UUID
    version: int
    status: str
    profile_hash: str | None
    validation_report: dict | None
    created_at: datetime
    published_at: datetime | None


class TreeNodeOut(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    path: str
    type: str


class TreeOut(BaseModel):
    snapshot_id: uuid.UUID
    version: int
    nodes: list[TreeNodeOut]


class SessionOut(ORM):
    id: uuid.UUID
    snapshot_id: uuid.UUID
    status: str
    runtime_id: str | None
    source_ip: str | None
    started_at: datetime
    ended_at: datetime | None


class EventOut(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    node_id: uuid.UUID | None
    event_type: str
    metadata: dict
    occurred_at: datetime


class GatewayEventOut(BaseModel):
    id: uuid.UUID
    connection_id: uuid.UUID
    session_id: uuid.UUID | None
    source_ip: str | None
    event_type: str
    metadata: dict  # attacker-supplied text inside: always escape when rendering
    occurred_at: datetime


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class AuditOut(ORM):
    id: uuid.UUID
    actor: str
    action: str
    target: str | None
    detail: dict
    occurred_at: datetime


class OverviewOut(BaseModel):
    snapshots: dict[str, int]
    sessions_total: int
    sessions_active: int
    events_total: int
    events_by_type: dict[str, int]
    published_version: int | None


class CreateSessionIn(BaseModel):
    snapshot_id: uuid.UUID | None = Field(default=None, description="Defaults to the published snapshot")


class ExecIn(BaseModel):
    command: str = Field(min_length=1, max_length=65_536)


class FsChangeOut(BaseModel):
    path: str
    operation: str
    kind: str
    size: int


class ExecOut(BaseModel):
    request_id: uuid.UUID
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration_ms: int
    runtime_killed: bool
    fs_changes: list[FsChangeOut]
    source: Literal["execution"] = "execution"  # raw evidence, never AI generated
