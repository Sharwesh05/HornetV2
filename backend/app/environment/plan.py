"""The environment plan: everything the builder decided, before anything is written to an image."""
import posixpath
from collections.abc import Callable
from dataclasses import dataclass, field

from app.graph import normalize_path

# The attacker can only write under these (tmpfs at runtime). Files the plan puts there are stored
# under SEED_BASE in the image and copied into the tmpfs when a twin starts.
SEED_ROOTS = ("/tmp", "/var/tmp", "/home/user")  # noqa: S108
SEED_BASE = "/usr/share/hornet/seed"
# Directories that already exist in the base image: they appear in the tree but are not re-created.
BASE_DIRS = frozenset(
    {"/", "/etc", "/var", "/var/log", "/var/lib", "/var/tmp", "/var/www", "/home", "/home/user", "/opt", "/srv",
     "/usr", "/usr/share", "/tmp", "/root", "/run", "/mnt"}  # noqa: S108
)

MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_FILES = 2000


def in_seed_root(path: str) -> bool:
    return any(path == r or path.startswith(r + "/") for r in SEED_ROOTS)


@dataclass
class FileSpec:
    path: str
    kind: str  # "dir" | "file"
    mode: int = 0o644
    uid: int = 0
    gid: int = 0
    content: str | None = None
    origin: str = "template"  # template | ai
    sensitivity: str = "public"  # public | synthetic-sensitive

    @property
    def image_path(self) -> str:
        return SEED_BASE + self.path if in_seed_root(self.path) and self.path not in SEED_ROOTS else self.path

    @property
    def exists_in_base(self) -> bool:
        return self.kind == "dir" and self.path in BASE_DIRS


@dataclass
class UserSpec:
    name: str
    uid: int
    gid: int
    home: str
    shell: str
    comment: str = ""
    kind: str = "service"  # system | service | human


@dataclass
class Person:
    first: str
    last: str
    email: str

    @property
    def full_name(self) -> str:
        return f"{self.first} {self.last}"


@dataclass
class ServicePlan:
    name: str
    role: str
    image: str
    ip: str
    ports: list[tuple[int, str]] = field(default_factory=list)
    user: str | None = None
    config_paths: list[str] = field(default_factory=list)
    log_paths: list[str] = field(default_factory=list)
    log_dir: str = ""
    root: str = ""
    loopback: str = ""  # address of its in-twin emulator, e.g. 127.0.1.11
    emulator: str = ""  # http | postgres | redis | '' (no emulator)


@dataclass
class DatabasePlan:
    name: str
    engine: str
    service: str
    owner: str
    tables: dict[str, list[dict]] = field(default_factory=dict)


@dataclass
class EnvPlan:
    hostname: str
    project: str
    files: dict[str, FileSpec] = field(default_factory=dict)
    users: dict[str, UserSpec] = field(default_factory=dict)
    services: list[ServicePlan] = field(default_factory=list)
    databases: list[DatabasePlan] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)
    secrets: dict[str, str] = field(default_factory=dict)  # fake secrets: name -> value
    expectations: list[tuple[str, str]] = field(default_factory=list)  # (file path, secret name)
    notes: dict = field(default_factory=dict)

    # -- files -----------------------------------------------------------------------------
    def add_dir(self, path: str, mode: int = 0o755, uid: int = 0, gid: int = 0) -> None:
        path = normalize_path(path)
        self.files.setdefault(path, FileSpec(path, "dir", mode, uid, gid))

    def add_file(
        self, path: str, content: str, mode: int = 0o644, uid: int = 0, gid: int = 0,
        origin: str = "template", sensitivity: str = "public",
    ) -> None:
        path = normalize_path(path)
        self.add_dir(posixpath.dirname(path) or "/")
        self.files[path] = FileSpec(path, "file", mode, uid, gid, content, origin, sensitivity)

    def ensure_parents(self) -> None:
        """Every file/dir needs all its ancestors in the tree, up to '/'."""
        for path in list(self.files):
            parent = posixpath.dirname(path)
            while parent and parent != path:
                self.files.setdefault(parent, FileSpec(parent, "dir", 0o755))
                if parent == "/":
                    break
                path, parent = parent, posixpath.dirname(parent)
        self.files.setdefault("/", FileSpec("/", "dir", 0o755))

    # -- secrets ---------------------------------------------------------------------------
    def secret(self, name: str, factory: Callable[[], str]) -> str:
        if name not in self.secrets:
            self.secrets[name] = factory()
        return self.secrets[name]

    def expect(self, path: str, secret_name: str) -> None:
        """Record that `path` must contain the fake secret `secret_name` (checked by the validator)."""
        self.expectations.append((normalize_path(path), secret_name))

    # -- users -----------------------------------------------------------------------------
    def add_user(self, user: UserSpec) -> UserSpec:
        return self.users.setdefault(user.name, user)

    def next_uid(self, start: int = 2000) -> int:
        used = {u.uid for u in self.users.values()}
        uid = start
        while uid in used:
            uid += 1
        return uid
