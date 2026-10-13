"""Execution policy: every limit that protects the host. It is built from server settings only,
and the hard maximums below stop a bad config from silently weakening isolation."""
from pydantic import BaseModel, ConfigDict, Field


class ExecutionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    timeout_ms: int = Field(5000, ge=100, le=60_000)
    output_limit_bytes: int = Field(65_536, ge=1024, le=1_048_576)
    input_limit_bytes: int = Field(8192, ge=16, le=65_536)
    memory_mb: int = Field(128, ge=16, le=1024)
    cpus: float = Field(0.5, gt=0, le=2)
    pids: int = Field(64, ge=8, le=512)
    tmpfs_mb: int = Field(16, ge=1, le=256)
    nofile: int = Field(256, ge=32, le=4096)
    max_active_sessions: int = Field(5, ge=1, le=100)
    session_ttl_s: int = Field(900, ge=10, le=86_400)
    idle_ttl_s: int = Field(300, ge=5, le=86_400)
    observe_fs: bool = True
    # Extra wall-clock slack before the driver gives up and kills the whole runtime.
    kill_grace_ms: int = Field(3000, ge=500, le=30_000)
