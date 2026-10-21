"""AI synthesis wired into sessions (Phase 5A): gaps flagged by the fake services become exactly one validated,
out-of-band ai_synthesis row each; the attacker's output and the raw evidence are untouched."""
import json
import time

import pytest
from sqlalchemy import select

from app.ai.provider import TemplateProvider
from app.ai.synthesis import SynthesisWorker
from app.config import Settings
from app.models import AiSynthesis, ExecutionObservation, HoneypotSession
from app.orchestration import session_manager as sm
from app.runtime.factory import build_synthesizer
from app.runtime.policy import ExecutionPolicy
from tests.fakes import FakeDriver, FakeProvider
from tests.helpers_synthesis import published_snapshot, worker

ANSWER = {"kind": "error", "code": "42P01", "message": 'relation "invoices" does not exist'}
BY_PROTOCOL = {"postgres": ANSWER, "redis": {"kind": "value", "value": "id=3 addr=127.0.0.1:50122 cmd=client"}}


def per_protocol(context, _schema):
    return BY_PROTOCOL[context["protocol"]]


@pytest.fixture
def snap(db):
    return published_snapshot(db)


def manager(session_factory, synthesizer=None) -> sm.SessionManager:
    return sm.SessionManager(FakeDriver(), session_factory, ExecutionPolicy(), "hornet-twin-base:2",
                             synthesizer=synthesizer)


def ai_rows(session_factory) -> list[AiSynthesis]:
    with session_factory() as v:
        return list(v.scalars(select(AiSynthesis)))


def test_each_unsupported_request_gets_exactly_one_ai_row_and_the_evidence_is_untouched(snap, session_factory):
    provider = FakeProvider(per_protocol)
    synth = worker(provider, session_factory)
    mgr = manager(session_factory, synth)
    s = mgr.create_session()
    plain_mgr = manager(session_factory)  # same command without AI: what the attacker sees must not differ
    plain = plain_mgr.create_session()

    outs = [mgr.execute(s.id, c) for c in ("gap postgres select * from invoices", "gap redis CLIENT LIST",
                                           "hit web", "echo done")]
    baseline = plain_mgr.execute(plain.id, "gap postgres select * from invoices")
    synth.drain()

    assert (outs[0].stdout, outs[0].stderr, outs[0].exit_code) == (baseline.stdout, baseline.stderr, baseline.exit_code)
    rows = ai_rows(session_factory)
    assert len(rows) == 2  # two unsupported requests; the served HTTP request and the echo produce none
    assert {r.request_id for r in rows} == {outs[0].request_id, outs[1].request_id}
    assert {r.validation_status for r in rows} == {"accepted"} and all(r.response_mode == "proposal" for r in rows)
    with session_factory() as v:
        evidence = json.dumps([o.result_metadata for o in v.scalars(select(ExecutionObservation))])
        assert v.get(HoneypotSession, s.id).ai_policy_version == "phase5a-synth-v1"
        assert v.get(HoneypotSession, plain.id).ai_policy_version is None
    assert "does not exist" not in evidence and '"source": "ai"' not in evidence
    assert all(o["source"] == "execution" for o in json.loads(evidence))
    synth.shutdown()


def test_without_synthesis_nothing_changes(snap, session_factory):
    mgr = manager(session_factory)
    s = mgr.create_session()
    out = mgr.execute(s.id, "gap postgres select * from invoices")
    assert out.exit_code == 1 and ai_rows(session_factory) == []
    with session_factory() as v:
        assert v.get(HoneypotSession, s.id).ai_policy_version is None


def test_the_attacker_never_waits_for_the_model(snap, session_factory):
    synth = worker(FakeProvider(ANSWER, delay=1.5), session_factory, timeout_s=3)
    mgr = manager(session_factory, synth)
    s = mgr.create_session()
    started = time.monotonic()
    mgr.execute(s.id, "gap postgres select * from invoices")
    assert time.monotonic() - started < 0.5  # the reply does not include the model's 1.5 s
    synth.drain()
    assert [r.validation_status for r in ai_rows(session_factory)] == ["accepted"]
    synth.shutdown()


def test_without_a_model_every_gap_is_recorded_as_fallback(snap, session_factory):
    synth = worker(TemplateProvider(), session_factory)
    mgr = manager(session_factory, synth)
    s = mgr.create_session()
    mgr.execute(s.id, "gap postgres select * from invoices")
    synth.drain()
    (row,) = ai_rows(session_factory)
    assert row.validation_status == "fallback" and row.response_metadata["reason"] == "no model provider configured"
    synth.shutdown()


def test_repeated_requests_hit_the_cache_and_the_session_cap_bounds_calls(snap, session_factory):
    provider = FakeProvider(ANSWER)
    synth = worker(provider, session_factory, max_per_session=2)
    mgr = manager(session_factory, synth)
    s = mgr.create_session()
    for sql in ("select * from invoices", "SELECT *   FROM invoices", "select 1 from a", "select 2 from b"):
        mgr.execute(s.id, f"gap postgres {sql}")
        synth.drain()
    rows = ai_rows(session_factory)
    statuses = [r.validation_status for r in rows]
    caches = [r.response_metadata["cache"] for r in rows]
    assert len(provider.calls) == 2                         # the second query was a cache hit; the 4th hit the cap
    assert statuses.count("fallback") == 1 and caches.count(True) == 1
    assert any(r.response_metadata["reason"] == "session cap reached" for r in rows)
    synth.shutdown()


def test_the_api_shows_ai_proposals_apart_from_the_evidence(client, snap, session_factory):
    from app.api.v1 import get_manager
    from app.main import app

    synth = worker(FakeProvider(ANSWER), session_factory)
    mgr = manager(session_factory, synth)
    app.dependency_overrides[get_manager] = lambda: mgr
    try:
        sid = client.post("/api/v1/sessions").json()["id"]
        client.post(f"/api/v1/sessions/{sid}/exec", json={"command": "gap postgres select * from invoices"})
        client.post(f"/api/v1/sessions/{sid}/exec", json={"command": "echo hi"})
        synth.drain()
        items = client.get(f"/api/v1/sessions/{sid}/executions").json()["items"]
    finally:
        app.dependency_overrides.pop(get_manager, None)
        synth.shutdown()
    gap, plain = items
    assert gap["source"] == "execution" and gap["stderr"] == "ERROR:  syntax error\n"  # the evidence, unchanged
    (proposal,) = gap["ai_proposals"]
    assert proposal["source"] == "ai" and proposal["served"] is False and proposal["status"] == "accepted"
    assert proposal["proposal"] == ANSWER and proposal["proto"] == "postgres" and proposal["service"] == "db"
    assert proposal["request"] == "select * from invoices" and plain["ai_proposals"] == []


def test_factory_wiring():
    base = {"admin_api_key": "x" * 20, "_env_file": None}
    assert build_synthesizer(Settings(**base), None) is None  # off by default
    offline = build_synthesizer(Settings(**base, ai_synth_enabled=True), None)
    assert isinstance(offline, SynthesisWorker) and isinstance(offline.provider, TemplateProvider)
    nim = build_synthesizer(Settings(**base, ai_synth_enabled=True, ai_provider="nim", nvidia_api_key="nvapi-" + "x" * 20,
                                     ai_synth_timeout_s=2.5, nim_synth_model="some/fast-model"), None)
    assert nim.provider.model == "some/fast-model" and nim.timeout_s == 2.5 and nim.provider._retries == 0
    assert nim.provider._thinking is False  # reasoning models answer in ~1 s instead of ~9 s
    assert nim.provider._guided_json is False  # schema-constrained decoding was 7x slower; the validator enforces it
    for w in (offline, nim):
        w.shutdown()
