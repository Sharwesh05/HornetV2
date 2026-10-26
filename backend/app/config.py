"""Validated settings. Unsafe configuration fails startup (AGENTS.md rule)."""
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> app -> backend -> repo root. The repo-root .env is found from any working
# directory; a .env in the current directory (listed last) still overrides it.
_REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILES = (_REPO_ROOT / ".env", Path(".env"))


def resolve_database_url(url: str) -> str:
    """Anchor a relative SQLite file path to the repo root so the API and alembic always open the
    same file, whatever directory they are started from. Other URLs are returned unchanged."""
    prefix = "sqlite:///"
    if not url.startswith(prefix) or url == prefix + ":memory:":
        return url
    raw = url[len(prefix):]
    # absolute = a leading slash on every OS (Path("/x").is_absolute() is False on Windows), or a Windows drive path
    if not raw or raw.startswith(("/", "\\")) or (len(raw) >= 2 and raw[1] == ":"):
        return url  # empty (memory), absolute, or a Windows drive path
    return prefix + (_REPO_ROOT / Path(raw)).as_posix()


log = logging.getLogger("hornet.config")

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# where secret files are read from: Docker/Kubernetes mount /run/secrets; systemd LoadCredential= uses
# $CREDENTIALS_DIRECTORY (see deployment/hornet.service)
SECRETS_DIR = os.environ.get("HORNET_SECRETS_DIR") or os.environ.get("CREDENTIALS_DIRECTORY") or "/run/secrets"
# the demo profile's ceilings: the defaults below; HORNET_ENV=dev may only make them tighter
DEMO_LIMITS = {"twin_timeout_ms": 5000, "twin_output_limit_bytes": 65536, "twin_memory_mb": 128, "twin_cpus": 0.5,
               "twin_pids": 64, "twin_tmpfs_mb": 16, "max_active_sessions": 5, "session_ttl_s": 900, "idle_ttl_s": 300}

def database_url_from_environment(default: str = "sqlite:///./hornet_dev.db") -> str:
    """The database URL without loading all settings (alembic needs no admin key): the environment variable, else a
    secret file `hornet_database_url` (Docker/Kubernetes secrets, systemd credentials), else the default."""
    url = os.environ.get("HORNET_DATABASE_URL")
    if not url:
        secret = Path(SECRETS_DIR) / "hornet_database_url"
        url = secret.read_text(encoding="utf-8").strip() if secret.is_file() else default
    return resolve_database_url(url)


_PLACEHOLDER_KEYS = {"change-me", "changeme", "admin", "password", "secret", "test"}


class Settings(BaseSettings):
    # Secrets may also come from files named like the setting (e.g. /run/secrets/hornet_admin_api_key), as mounted by
    # Docker or Kubernetes secrets; environment variables and .env still take precedence.
    model_config = SettingsConfigDict(env_prefix="HORNET_", env_file=ENV_FILES, env_file_encoding="utf-8-sig",
                                      extra="ignore", secrets_dir=SECRETS_DIR if Path(SECRETS_DIR).is_dir() else None)

    env: Literal["dev", "prod"] = "dev"
    database_url: str = "sqlite:///./hornet_dev.db"
    admin_api_key: str  # no default: must be provided
    page_size_default: int = 50
    page_size_max: int = 200

    # --- twin runtime (server-side policy; never taken from attacker input) ---
    runtime_driver: Literal["docker", "gvisor"] = "docker"  # gvisor: Docker's runsc runtime (Linux), see Phase H6
    twin_base_image: str = "hornet-twin-base:2"
    twin_timeout_ms: int = 5000
    twin_output_limit_bytes: int = 65536
    twin_memory_mb: int = 128
    twin_cpus: float = 0.5
    twin_pids: int = 64
    twin_tmpfs_mb: int = 16
    max_active_sessions: int = 5
    session_ttl_s: int = 900
    idle_ttl_s: int = 300

    # --- discovery and environment builds ---
    discovery_allowed_projects: str = ""  # comma-separated compose projects Hornet may observe; empty = none
    build_dir: str = "./data/builds"

    # --- AI: NVIDIA NIM (OpenAI-compatible; hosted or a self-hosted NIM container) ---
    ai_provider: Literal["template", "nim"] = "template"  # template = offline, deterministic, no model
    # The key is read from the environment / .env only, never from code or the API. Either name works.
    nvidia_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("HORNET_NVIDIA_API_KEY", "NVIDIA_API_KEY")
    )
    nim_base_url: str = "https://integrate.api.nvidia.com/v1"
    nim_model: str = "nvidia/nemotron-3-super-120b-a12b"  # run GET /v1/models to see what your key can use
    nim_timeout_s: float = Field(default=90.0, gt=0, le=300)
    nim_max_tokens: int = Field(default=4096, ge=256, le=32768)
    # How the AI sanitizer behaves (it runs AFTER the deterministic redaction, never instead of it):
    #   required = the build fails if the model cannot review the data
    #   advisory = a model failure is recorded and the (already clean) deterministic result is used
    #   off      = skip the model review
    ai_sanitizer_mode: Literal["off", "advisory", "required"] = "required"
    # AI synthesis (Phase 5A): after a command, requests the fake services flagged `unsupported` get an AI proposal,
    # validated in code and stored apart from the evidence. It is never sent to the attacker (out of band).
    ai_synth_enabled: bool = False
    ai_synth_timeout_s: float = Field(default=3.0, ge=0.2, le=30)  # hard limit per model call; then "timeout"
    ai_synth_max_per_session: int = Field(default=20, ge=1, le=500)  # model calls per session; then "fallback"
    # measured (docs/results/synthesis_latency.md): with thinking off and no guided decoding, ~1 s median
    nim_synth_model: str = "nvidia/nemotron-3-super-120b-a12b"

    # --- attacker-facing SSH gateway (off by default; binds to loopback unless you change it on purpose) ---
    gateway_enabled: bool = False
    gateway_host: str = "127.0.0.1"
    gateway_port: int = Field(default=2222, ge=1, le=65535)
    gateway_host_key_path: str = "./data/gateway/ssh_host_ed25519_key"
    gateway_server_version: str = "OpenSSH_9.2p1 Debian-2+deb12u3"
    gateway_accept_after: int = Field(default=3, ge=1, le=6)  # a login succeeds on this attempt of a connection
    gateway_weak_passwords: str = "123456,password,admin,root,toor"  # accepted on the first try
    gateway_log_passwords: bool = False  # off: length + short hash only; on: the attempted password text is stored
    gateway_max_connections_per_ip: int = Field(default=3, ge=1, le=50)
    gateway_login_timeout_s: int = Field(default=30, ge=5, le=300)

    # --- admin dashboard (frontend/): browser origins allowed to call the API and open the live feed ---
    dashboard_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    # --- recorded attacker data (README "Command storage") ---
    store_raw_commands: bool = False  # commands are always stored with credentials redacted; this adds the raw text
    retention_days: int = Field(default=30, ge=1, le=3650)  # `python -m scripts.dev purge` deletes older sessions
    retention_auto: bool = False  # also purge automatically, once an hour (required in prod)

    def weak_passwords(self) -> set[str]:
        return {p for p in self.gateway_weak_passwords.split(",") if p}

    def dashboard_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.dashboard_origins.split(",") if o.strip()]

    def allowed_projects(self) -> set[str]:
        return {p.strip() for p in self.discovery_allowed_projects.split(",") if p.strip()}

    @field_validator("database_url")
    @classmethod
    def _anchor_sqlite(cls, v: str) -> str:
        return resolve_database_url(v)

    @field_validator("admin_api_key")
    @classmethod
    def _strong_key(cls, v: str) -> str:
        if len(v) < 16 or v.lower() in _PLACEHOLDER_KEYS:
            raise ValueError("HORNET_ADMIN_API_KEY must be at least 16 chars and not a placeholder")
        return v

    @model_validator(mode="after")
    def _prod_rules(self) -> "Settings":
        if self.env == "prod" and self.database_url.startswith("sqlite"):
            raise ValueError("SQLite is not allowed when HORNET_ENV=prod")
        if self.env == "prod":
            self._prod_profile()
        self.execution_policy()  # raises if any limit is outside the hard bounds
        if self.gateway_enabled and self.gateway_host not in ("127.0.0.1", "::1", "localhost") and self.env != "prod":
            raise ValueError("the SSH gateway may only bind to loopback outside HORNET_ENV=prod (AGENTS.md rule 10)")
        if any(o == "*" or not o.startswith(("http://", "https://")) for o in self.dashboard_origin_list()):
            raise ValueError("HORNET_DASHBOARD_ORIGINS must list explicit http(s) origins; '*' is not allowed")
        if self.env == "dev":
            self._demo_safety()
        if self.ai_provider == "nim":
            if self.nvidia_api_key is None or len(self.nvidia_api_key.get_secret_value()) < 10:
                raise ValueError("HORNET_AI_PROVIDER=nim needs NVIDIA_API_KEY (put it in .env, never in code)")
            if not self.nim_base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
                raise ValueError("HORNET_NIM_BASE_URL must be https (plain http is allowed for localhost only)")
        return self

    def _prod_profile(self) -> None:
        """HORNET_ENV=prod (Phase H3): a deliberate deployment. Unsafe choices fail startup; sensitive-but-legitimate
        research choices are allowed with a warning in the log."""
        problems = []
        if len(self.admin_api_key) < 32:
            problems.append("HORNET_ADMIN_API_KEY must be at least 32 characters in prod")
        if any(not o.startswith("https://") for o in self.dashboard_origin_list()):
            problems.append("dashboard origins must be https:// in prod (HORNET_DASHBOARD_ORIGINS)")
        if self.ai_provider == "nim" and self.ai_sanitizer_mode != "required":
            problems.append("with an AI model, HORNET_AI_SANITIZER_MODE must be 'required' in prod")
        if not self.retention_auto:
            problems.append("automatic retention must be on in prod (HORNET_RETENTION_AUTO=true)")
        if problems:
            raise ValueError("unsafe production configuration: " + "; ".join(problems))
        if self.store_raw_commands:
            log.warning("HORNET_STORE_RAW_COMMANDS is on in prod: unredacted attacker input (possibly third-party "
                        "credentials) will be stored")
        if self.gateway_log_passwords:
            log.warning("HORNET_GATEWAY_LOG_PASSWORDS is on in prod: attempted passwords are stored as text")

    def _demo_safety(self) -> None:
        """HORNET_ENV=dev is the demo profile on a developer machine (AGENTS.md rule 10). It may tighten anything but
        loosen nothing: everything stays on loopback, raw attacker input is never stored, and no twin limit is looser
        than its default. Anything else needs a deliberate HORNET_ENV=prod deployment (Phase H)."""
        problems = []
        for origin in self.dashboard_origin_list():
            host = origin.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0].strip("[]")
            if host not in LOOPBACK_HOSTS:
                problems.append(f"dashboard origin {origin} is not on this machine (HORNET_DASHBOARD_ORIGINS)")
        if self.store_raw_commands:
            problems.append("raw attacker input must not be stored in the demo profile (HORNET_STORE_RAW_COMMANDS)")
        for name, ceiling in DEMO_LIMITS.items():
            if getattr(self, name) > ceiling:
                problems.append(f"HORNET_{name.upper()}={getattr(self, name)} is looser than the demo limit {ceiling}")
        if problems:
            raise ValueError("unsafe demo configuration (HORNET_ENV=dev): " + "; ".join(problems))

    def safety_summary(self) -> dict[str, str]:
        """What `python -m scripts.dev safety` prints: the settings that keep the demo safe, as they are now."""
        policy = self.execution_policy()
        return {
            "profile": self.env,
            "twin runtime": ("gVisor (runsc): own user-space kernel" if self.runtime_driver == "gvisor"
                             else "hardened Docker (runc): shares the host kernel"),
            "ssh gateway": f"{'on' if self.gateway_enabled else 'off'}, {self.gateway_host}:{self.gateway_port}",
            "dashboard origins": ", ".join(self.dashboard_origin_list()),
            "twin limits": (f"{policy.timeout_ms} ms per command, {policy.memory_mb} MB, {policy.cpus} CPU, "
                            f"{policy.pids} processes, {policy.tmpfs_mb} MB writable, {policy.output_limit_bytes} B output"),
            "sessions": f"at most {policy.max_active_sessions} at once, {policy.session_ttl_s} s max, {policy.idle_ttl_s} s idle",
            "commands stored": "redacted only" if not self.store_raw_commands else "redacted + RAW",
            "login passwords stored": "as text" if self.gateway_log_passwords else "length + hash only",
            "retention": f"{self.retention_days} days ({'automatic, hourly' if self.retention_auto else 'manual: python -m scripts.dev purge'})",
            "ai": f"{self.ai_provider}; sanitizer review {self.sanitizer_mode}; synthesis {'on' if self.ai_synth_enabled else 'off'}",
        }

    @property
    def sanitizer_mode(self) -> str:
        """The template provider has no judgement to apply, so the AI review only exists with a model."""
        return self.ai_sanitizer_mode if self.ai_provider == "nim" else "off"

    def execution_policy(self):
        from app.runtime.policy import ExecutionPolicy  # bounds are validated there

        return ExecutionPolicy(
            timeout_ms=self.twin_timeout_ms,
            output_limit_bytes=self.twin_output_limit_bytes,
            memory_mb=self.twin_memory_mb,
            cpus=self.twin_cpus,
            pids=self.twin_pids,
            tmpfs_mb=self.twin_tmpfs_mb,
            max_active_sessions=self.max_active_sessions,
            session_ttl_s=self.session_ttl_s,
            idle_ttl_s=self.idle_ttl_s,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # admin_api_key comes from the environment or .env
