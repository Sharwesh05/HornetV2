"""Server profile types.

Raw* types hold what discovery observed and exist only in memory, between discovery and
sanitization. Note that they carry environment variable NAMES only: values are dropped at the source.
Sanitized* types are the only thing that may be stored, shown to an admin, or sent to an AI provider.
"""
from dataclasses import dataclass, field

from pydantic import BaseModel, Field


# ---- raw (never persisted, never logged) --------------------------------------------------------
@dataclass
class RawPort:
    container_port: int
    protocol: str = "tcp"
    host_ip: str | None = None
    host_port: int | None = None


@dataclass
class RawMount:
    kind: str  # bind | volume | tmpfs
    destination: str
    read_only: bool = False
    # NOTE: the host-side source path is deliberately not collected.


@dataclass
class RawService:
    name: str
    container_name: str
    image: str
    env_names: list[str] = field(default_factory=list)  # names only, never values
    entrypoint: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    ports: list[RawPort] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    mounts: list[RawMount] = field(default_factory=list)
    user: str | None = None
    workdir: str | None = None
    healthcheck: str | None = None
    fs_paths: list[str] = field(default_factory=list)


@dataclass
class RawEnvironment:
    project: str
    services: list[RawService]
    networks: list[str] = field(default_factory=list)


# ---- sanitized (safe to store / review / send to AI) ----------------------------------------------
class PortSpec(BaseModel):
    container_port: int
    protocol: str = "tcp"
    host_port: int | None = None
    bind: str | None = None


class MountSpec(BaseModel):
    destination: str
    kind: str
    read_only: bool = False


class ServiceSpec(BaseModel):
    name: str
    role: str = "generic"
    image: str
    ports: list[PortSpec] = Field(default_factory=list)
    env_names: list[str] = Field(default_factory=list)
    env_classes: dict[str, str] = Field(default_factory=dict)  # name -> secret|connection|config
    networks: list[str] = Field(default_factory=list)
    mounts: list[MountSpec] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    entrypoint: list[str] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)
    healthcheck: str | None = None
    user: str | None = None
    workdir: str | None = None
    fs_hints: list[str] = Field(default_factory=list)


class SanitizationReport(BaseModel):
    """Counts only. Never contains the removed values."""

    findings_by_kind: dict[str, int] = Field(default_factory=dict)
    dropped_env_values: int = 0
    dropped_labels: int = 0
    dropped_mount_sources: int = 0
    dropped_paths: int = 0
    ai_review: dict = Field(default_factory=dict)  # counts only; see sanitization/ai_review.py


class SanitizedProfile(BaseModel):
    schema_version: int = 1
    source: str
    services: list[ServiceSpec]
    networks: list[str] = Field(default_factory=list)
    report: SanitizationReport = Field(default_factory=SanitizationReport)
    profile_hash: str = ""
