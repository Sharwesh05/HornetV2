"""Runtime driver contract. The orchestrator only talks to this interface, so the same flow works
for hardened Docker (Windows/Linux dev), gVisor/Kata, or Firecracker microVMs later."""
from dataclasses import dataclass, field
from typing import Protocol

from app.runtime.policy import ExecutionPolicy


class RuntimeError_(Exception):  # noqa: N801  (avoid shadowing the builtin)
    """Base class for runtime failures."""


class RuntimeUnavailable(RuntimeError_):
    """The runtime backend (e.g. Docker daemon) cannot be reached."""


class ImageMismatch(RuntimeError_):
    """The image to launch is missing or does not match the pinned digest."""


class RuntimeKilled(RuntimeError_):
    """The runtime had to be terminated (limit exceeded / unresponsive)."""


@dataclass(frozen=True)
class RuntimeSpec:
    image: str
    session_id: str
    hostname: str
    attacker_user: str = "1000:1000"  # runs every attacker command
    system_user: str = "1001:1001"  # owns PID 1; attacker processes cannot signal it
    workdir: str = "/home/user"
    env: dict[str, str] = field(default_factory=dict)
    # The only places the attacker can write. Everything else is a read-only root filesystem.
    writable_paths: tuple[str, ...] = ("/tmp", "/var/tmp", "/home/user")  # noqa: S108
    # service name -> loopback address of its in-twin emulator (e.g. {'db': '127.0.1.11'})
    extra_hosts: dict[str, str] = field(default_factory=dict)


@dataclass
class ExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration_ms: int
    runtime_killed: bool = False


@dataclass(frozen=True)
class FsEntry:
    path: str
    kind: str  # f, d, l, ...
    size: int
    mtime: float


class RuntimeDriver(Protocol):
    name: str

    def ping(self) -> bool: ...

    def resolve_image(self, ref: str) -> str:
        """Return the immutable digest/ID of an image, or raise ImageMismatch."""

    def create(self, spec: RuntimeSpec, policy: ExecutionPolicy) -> str:
        """Create and start an isolated runtime; return its runtime_id."""

    def exec(self, runtime_id: str, spec: RuntimeSpec, command: str, policy: ExecutionPolicy) -> ExecResult: ...

    def snapshot_fs(self, runtime_id: str, spec: RuntimeSpec, policy: ExecutionPolicy) -> list[FsEntry]: ...

    def processes(self, runtime_id: str) -> list[dict]: ...

    def is_running(self, runtime_id: str) -> bool: ...

    def start_services(self, runtime_id: str, spec: RuntimeSpec, policy: ExecutionPolicy) -> str:
        """Start the fake services baked into the image. Returns 'started', 'none' (image has none) or 'failed'."""

    def collect_service_events(self, runtime_id: str, spec: RuntimeSpec, offset: int) -> tuple[list[dict], int]:
        """Events the fake services logged since `offset`; returns (events, new_offset)."""

    def destroy(self, runtime_id: str) -> None:
        """Idempotent: destroying a missing runtime is not an error."""

    def list_managed(self) -> list[str]:
        """Runtime ids this driver created (used to clean up orphans after a crash)."""
