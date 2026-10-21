"""Validated settings. Unsafe configuration fails startup (AGENTS.md rule)."""
import logging
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
    path = Path(url[len(prefix):])
    if not path.parts or path.is_absolute() or (len(path.parts[0]) == 2 and path.parts[0][1] == ":"):
        return url  # empty (memory), absolute, or a Windows drive path
    return prefix + (_REPO_ROOT / path).as_posix()


log = logging.getLogger("hornet.config")

_PLACEHOLDER_KEYS = {"change-me", "changeme", "admin", "password", "secret", "test"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HORNET_", env_file=ENV_FILES, env_file_encoding="utf-8-sig", extra="ignore")

    env: Literal["dev", "prod"] = "dev"
    database_url: str = "sqlite:///./hornet_dev.db"
    admin_api_key: str  # no default: must be provided
    page_size_default: int = 50
    page_size_max: int = 200

    # --- twin runtime (server-side policy; never taken from attacker input) ---
    runtime_driver: Literal["docker"] = "docker"
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
        if self.env == "prod" and self.store_raw_commands:
            log.warning("HORNET_STORE_RAW_COMMANDS is on in prod: unredacted attacker input (possibly third-party "
                        "credentials) will be stored")
        self.execution_policy()  # raises if any limit is outside the hard bounds
        if self.gateway_enabled and self.gateway_host not in ("127.0.0.1", "::1", "localhost") and self.env != "prod":
            raise ValueError("the SSH gateway may only bind to loopback outside HORNET_ENV=prod (AGENTS.md rule 10)")
        if any(o == "*" or not o.startswith(("http://", "https://")) for o in self.dashboard_origin_list()):
            raise ValueError("HORNET_DASHBOARD_ORIGINS must list explicit http(s) origins; '*' is not allowed")
        if self.ai_provider == "nim":
            if self.nvidia_api_key is None or len(self.nvidia_api_key.get_secret_value()) < 10:
                raise ValueError("HORNET_AI_PROVIDER=nim needs NVIDIA_API_KEY (put it in .env, never in code)")
            if not self.nim_base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
                raise ValueError("HORNET_NIM_BASE_URL must be https (plain http is allowed for localhost only)")
        return self

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
    return Settings()  # type: ignore[call-arg]
