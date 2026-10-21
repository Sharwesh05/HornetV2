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
    last_activity_at: datetime | None = None
    end_reason: str | None = None
    ai_policy_version: str | None = None
    commands: int = 0  # commands the session ran (list and detail endpoints)
    ai_gaps: int = 0  # AI proposals recorded for requests the fake services could not answer


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


class AiProposalOut(BaseModel):
    """An AI proposal for a request a fake service could not answer (Phase 5A). NOT evidence and NOT shown to the
    attacker: they received the fake service's controlled error. Keep it visually apart from the raw output."""

    id: uuid.UUID
    source: Literal["ai"] = "ai"
    served: bool = False  # 5A: never served
    status: str  # accepted | rejected | timeout | fallback
    reason: str
    service: str | None
    proto: str | None
    request: str | None  # the attacker's request, credentials redacted
    proposal: dict | None  # only for accepted proposals
    provider: str | None
    policy_version: str | None
    latency_ms: int | None
    cache: bool = False


class ExecutionOut(BaseModel):
    """One recorded command: what was typed (credentials redacted) and the raw execution evidence. Attacker-controlled
    text throughout: always render as plain text."""

    request_id: uuid.UUID
    operation: str
    command: str | None  # redacted input; None for rows recorded before command storage existed
    command_raw: str | None  # only when HORNET_STORE_RAW_COMMANDS=true
    input_hash: str
    created_at: datetime
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration_ms: int | None
    runtime_killed: bool
    source: Literal["execution"] = "execution"  # raw evidence, never AI generated
    ai_proposals: list[AiProposalOut] = []  # separate from the evidence above


class OverlayOut(BaseModel):
    """A file the attacker created, modified or deleted (from the filesystem diff). Paths only, no contents."""

    id: uuid.UUID
    path: str
    operation: str
    kind: str | None
    size: int | None
    request_id: str | None


class TreeStateNodeOut(BaseModel):
    """A node of the session's view of the environment: the immutable base, what the attacker visited, and the
    session's overlay (file changes). Nodes created only by the attacker have no id (they are not in the base)."""

    id: uuid.UUID | None
    parent_id: uuid.UUID | None
    path: str  # attacker-controlled for overlay-only nodes: render as text
    type: str
    in_base: bool
    # public | synthetic-sensitive (holds the twin's fake credentials) | attacker-created (overlay-only nodes)
    sensitivity: str = "public"
    visits: int  # session events linked to this node
    basis: list[str]  # why it counts as visited: command-argument | filesystem-diff | service-request
    overlay: str | None  # created | modified | deleted (latest), if the session changed it


class TreeStateOut(BaseModel):
    session_id: uuid.UUID
    snapshot_id: uuid.UUID
    version: int
    nodes: list[TreeStateNodeOut]
