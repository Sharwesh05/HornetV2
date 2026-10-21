"""Phase 7.5: the demo profile (HORNET_ENV=dev) may tighten settings but never loosen them; startup fails otherwise."""
import pytest
from pydantic import ValidationError

from app.config import DEMO_LIMITS, Settings

BASE = {"admin_api_key": "x" * 20, "_env_file": None}


def test_defaults_are_the_safe_demo_profile():
    s = Settings(**BASE)
    assert s.env == "dev" and s.gateway_host == "127.0.0.1" and s.store_raw_commands is False
    summary = s.safety_summary()
    assert summary["commands stored"] == "redacted only" and summary["login passwords stored"] == "length + hash only"
    assert "127.0.0.1:2222" in summary["ssh gateway"]


@pytest.mark.parametrize("override,fragment", [
    ({"gateway_enabled": True, "gateway_host": "0.0.0.0"}, "loopback"),  # noqa: S104
    ({"dashboard_origins": "http://192.168.1.20:5173"}, "dashboard origin"),
    ({"dashboard_origins": "http://127.0.0.1:5173,https://dashboard.example.com"}, "dashboard origin"),
    ({"store_raw_commands": True}, "raw attacker input"),
    ({"twin_timeout_ms": 20000}, "HORNET_TWIN_TIMEOUT_MS"),
    ({"twin_memory_mb": 512}, "HORNET_TWIN_MEMORY_MB"),
    ({"twin_pids": 256}, "HORNET_TWIN_PIDS"),
    ({"twin_cpus": 1.0}, "HORNET_TWIN_CPUS"),
    ({"max_active_sessions": 20}, "HORNET_MAX_ACTIVE_SESSIONS"),
    ({"idle_ttl_s": 3600}, "HORNET_IDLE_TTL_S"),
])
def test_loosening_anything_in_the_demo_profile_fails_startup(override, fragment):
    with pytest.raises(ValidationError) as exc:
        Settings(**BASE, **override)
    assert fragment in str(exc.value)


def test_tightening_is_always_allowed():
    s = Settings(**BASE, twin_timeout_ms=2000, twin_memory_mb=64, twin_pids=32, max_active_sessions=2, idle_ttl_s=60,
                 dashboard_origins="http://localhost:5174,http://[::1]:5173")
    assert s.execution_policy().memory_mb == 64


def test_every_limit_has_a_demo_ceiling_equal_to_its_default():
    defaults = Settings(**BASE)
    for name, ceiling in DEMO_LIMITS.items():
        assert getattr(defaults, name) == ceiling, name


def test_prod_is_a_separate_deliberate_profile():
    # outside the demo profile these choices are possible, but prod has its own rules (no SQLite, Phase H review)
    with pytest.raises(ValidationError):
        Settings(**BASE, env="prod")  # still SQLite
    s = Settings(**BASE, env="prod", database_url="postgresql+psycopg://h:p@db/h", store_raw_commands=True)
    assert s.safety_summary()["commands stored"] == "redacted + RAW"
