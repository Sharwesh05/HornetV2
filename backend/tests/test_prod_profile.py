"""Phase H3: the production profile, secrets from files, automatic retention, and auditing of the admin console."""
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings
from app.main import run_retention
from app.models import AdminAuditEvent, HoneypotSession

PROD = {"admin_api_key": "k" * 40, "_env_file": None, "env": "prod", "database_url": "postgresql+psycopg://h:p@db/h",
        "dashboard_origins": "https://hornet.example.org", "retention_auto": True}


def test_a_complete_prod_configuration_starts():
    s = Settings(**PROD)
    assert s.env == "prod" and s.safety_summary()["retention"].startswith("30 days (automatic")


@pytest.mark.parametrize("override,fragment", [
    ({"admin_api_key": "k" * 20}, "at least 32 characters"),
    ({"dashboard_origins": "http://hornet.example.org"}, "https://"),
    ({"dashboard_origins": "https://ok.example.org,http://127.0.0.1:5173"}, "https://"),
    ({"retention_auto": False}, "automatic retention"),
    ({"ai_provider": "nim", "nvidia_api_key": "nvapi-" + "x" * 20, "ai_sanitizer_mode": "advisory"}, "'required'"),
    ({"database_url": "sqlite:///./x.db"}, "SQLite"),
])
def test_unsafe_prod_choices_fail_startup(override, fragment):
    with pytest.raises(ValidationError) as exc:
        Settings(**{**PROD, **override})
    assert fragment in str(exc.value)


def test_sensitive_research_choices_are_allowed_in_prod_but_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="hornet.config"):
        Settings(**{**PROD, "store_raw_commands": True, "gateway_log_passwords": True})
    assert "HORNET_STORE_RAW_COMMANDS" in caplog.text and "HORNET_GATEWAY_LOG_PASSWORDS" in caplog.text


def test_secrets_can_come_from_mounted_secret_files(tmp_path, monkeypatch):
    monkeypatch.delenv("HORNET_ADMIN_API_KEY", raising=False)
    (tmp_path / "hornet_admin_api_key").write_text("from-a-secret-file-0123456789", encoding="utf-8")
    assert Settings(_env_file=None, _secrets_dir=tmp_path).admin_api_key == "from-a-secret-file-0123456789"


def test_automatic_retention_uses_the_audited_purge(db, session_factory):
    old = HoneypotSession(snapshot_id=uuid.uuid4(), status="destroyed", started_at=datetime.now(UTC) - timedelta(days=50),
                          ended_at=datetime.now(UTC) - timedelta(days=45))
    db.add(old)
    db.commit()
    counts = run_retention(30, session_factory)
    assert counts["honeypot_session"] == 1
    with session_factory() as v:
        audit = v.scalars(select(AdminAuditEvent).where(AdminAuditEvent.action == "retention.purge")).one()
        assert audit.actor == "retention-auto"


def test_the_admin_console_is_audited_without_the_command(client, session_factory):
    from app.api.v1 import get_manager
    from app.main import app
    from app.orchestration.session_manager import SessionManager
    from app.runtime.policy import ExecutionPolicy
    from tests.fakes import FakeDriver

    mgr = SessionManager(FakeDriver(), session_factory, ExecutionPolicy(), "hornet-twin-base:2")
    app.dependency_overrides[get_manager] = lambda: mgr
    try:
        snap = client.post("/api/v1/environments/build").json()
        client.post(f"/api/v1/environments/{snap['id']}/validate")
        client.post(f"/api/v1/environments/{snap['id']}/publish")
        sid = client.post("/api/v1/sessions").json()["id"]
        out = client.post(f"/api/v1/sessions/{sid}/exec", json={"command": "echo SECRET-ADMIN-TYPED-77"}).json()
        client.post(f"/api/v1/sessions/{uuid.uuid4()}/exec", json={"command": "id"})
    finally:
        app.dependency_overrides.pop(get_manager, None)
    with session_factory() as v:
        audits = {a.action: a for a in v.scalars(select(AdminAuditEvent))}
    assert audits["session.exec"].target == sid and audits["session.exec"].detail["request_id"] == out["request_id"]
    assert "SECRET-ADMIN-TYPED-77" not in str(audits["session.exec"].detail)
    assert audits["session.exec_refused"].detail["reason"] == "SessionNotFound"
