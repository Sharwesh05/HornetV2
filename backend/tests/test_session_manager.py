import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import services
from app.models import (
    ExecutionObservation,
    ExecutionRequest,
    HoneypotSession,
    SessionEvent,
    SessionOverlay,
    TwinImage,
)
from app.orchestration import lifecycle
from app.orchestration import session_manager as sm
from app.runtime.driver import ImageMismatch
from app.runtime.policy import ExecutionPolicy
from tests.fakes import FakeDriver


@pytest.fixture
def published(db):
    snap = services.create_draft_snapshot(db, None)
    services.validate_snapshot(db, snap)
    services.publish_snapshot(db, snap)
    db.commit()
    return snap


def make(session_factory, **policy):
    driver = FakeDriver()
    mgr = sm.SessionManager(driver, session_factory, ExecutionPolicy(**policy), "hornet-twin-base:2")
    return mgr, driver


def test_requires_published_snapshot(db, session_factory):
    mgr, _ = make(session_factory)
    draft = services.create_draft_snapshot(db, None)
    db.commit()
    with pytest.raises(sm.SnapshotNotPublished):
        mgr.create_session(draft.id)
    with pytest.raises(sm.SnapshotNotPublished):
        mgr.create_session()  # nothing published at all


def test_lifecycle_and_evidence(db, published, session_factory):
    mgr, driver = make(session_factory)
    sess = mgr.create_session(source_ip="203.0.113.7")
    assert sess.status == "ready" and sess.runtime_id in driver.runtimes
    assert driver.runtimes[sess.runtime_id]["spec"].image == "hornet-twin-base:2"

    out = mgr.execute(sess.id, "echo hello")
    assert (out.exit_code, out.stdout) == (0, "hello\n")
    assert [c["operation"] for c in out.fs_changes] == ["created"]

    with session_factory() as v:
        assert v.get(HoneypotSession, sess.id).status == "active"
        req = v.scalars(select(ExecutionRequest)).one()
        assert len(req.input_hash) == 64 and req.input_text == "echo hello"  # redacted text (nothing to redact here)
        assert req.input_raw is None  # the raw input is opt-in
        obs = v.scalars(select(ExecutionObservation)).one()
        assert obs.result_metadata["source"] == "execution" and obs.result_metadata["stdout"] == "hello\n"
        assert v.scalars(select(SessionOverlay)).one().operation == "created"
        types = [e.event_type for e in v.scalars(select(SessionEvent).order_by(SessionEvent.occurred_at))]
        assert {"session.provisioning", "session.ready", "session.active", "command_executed"} <= set(types)

    done = mgr.destroy_session(sess.id)
    assert done.status == "destroyed" and done.ended_at and sess.runtime_id in driver.destroyed
    assert mgr.destroy_session(sess.id).status == "destroyed"  # idempotent
    with pytest.raises(sm.SessionNotUsable):
        mgr.execute(sess.id, "echo again")


def test_session_limit_and_slot_release(db, published, session_factory):
    mgr, _ = make(session_factory, max_active_sessions=2)
    a, b = mgr.create_session(), mgr.create_session()
    with pytest.raises(sm.SessionLimitReached):
        mgr.create_session()
    mgr.destroy_session(a.id)
    assert mgr.create_session().status == "ready"
    assert b.id != a.id


def test_sessions_get_separate_runtimes(db, published, session_factory):
    mgr, driver = make(session_factory)
    a, b = mgr.create_session(), mgr.create_session()
    mgr.execute(a.id, "touch /tmp/secret")
    assert "/tmp/secret" in driver.runtimes[a.runtime_id]["files"]
    assert "/tmp/secret" not in driver.runtimes[b.runtime_id]["files"]
    assert a.runtime_id != b.runtime_id


@pytest.mark.parametrize("bad", ["", "a" * 9000, "ls\x00rm"])
def test_input_rejected(db, published, session_factory, bad):
    mgr, _ = make(session_factory)
    s = mgr.create_session()
    with pytest.raises(sm.InputRejected):
        mgr.execute(s.id, bad)


def test_timeout_and_truncation_are_flagged_not_hidden(db, published, session_factory):
    mgr, _ = make(session_factory, timeout_ms=1000, output_limit_bytes=2048)
    s = mgr.create_session()
    t = mgr.execute(s.id, "sleep-forever")
    assert t.timed_out and t.exit_code == 137
    f = mgr.execute(s.id, "flood")
    assert f.truncated and len(f.stdout) <= 2048


def test_limit_violation_kills_twin_and_fails_session(db, published, session_factory):
    mgr, driver = make(session_factory)
    s = mgr.create_session()
    out = mgr.execute(s.id, "bomb")
    assert out.runtime_killed
    assert s.runtime_id in driver.destroyed
    with session_factory() as v:
        row = v.get(HoneypotSession, s.id)
        assert row.status == "failed" and "limit" in row.end_reason
    with pytest.raises(sm.SessionNotUsable):
        mgr.execute(s.id, "echo hi")
    assert mgr.active_count() == 0
    assert mgr.destroy_session(s.id).status == "destroyed"


@pytest.mark.parametrize("command", ["saturate", "cannot-fork"])
def test_a_twin_that_ran_out_of_processes_is_failed_not_left_wedged(db, published, session_factory, command):
    """Regression: after a fork bomb the twin used to stay 'active' while every command failed with exit 125."""
    mgr, driver = make(session_factory)
    s = mgr.create_session()
    out = mgr.execute(s.id, command)
    assert out.runtime_killed and out.fs_changes == []
    assert s.runtime_id in driver.destroyed and mgr.active_count() == 0
    with session_factory() as v:
        row = v.get(HoneypotSession, s.id)
        assert row.status == "failed" and row.end_reason == "limit exceeded: process limit"
    with pytest.raises(sm.SessionNotUsable):
        mgr.execute(s.id, "echo hi")


def test_a_twin_whose_sandbox_stopped_after_the_command_is_failed(db, published, session_factory):
    """Regression (gVisor): the fork bomb returns exit 0 at once and the sandbox dies a moment later."""
    mgr, driver = make(session_factory)
    s = mgr.create_session()
    out = mgr.execute(s.id, "sandbox-dies")
    assert out.runtime_killed and s.runtime_id in driver.destroyed and mgr.active_count() == 0
    with session_factory() as v:
        assert v.get(HoneypotSession, s.id).end_reason == "limit exceeded: runtime stopped"
    with pytest.raises(sm.SessionNotUsable):
        mgr.execute(s.id, "echo hi")


def test_a_busy_but_healthy_twin_is_not_mistaken_for_exhausted(db, published, session_factory):
    mgr, driver = make(session_factory)
    s = mgr.create_session()
    driver.runtimes[s.runtime_id]["procs"] = ExecutionPolicy().pids - 5  # busy, not full
    assert not mgr.execute(s.id, "echo hi").runtime_killed
    with session_factory() as v:
        assert v.get(HoneypotSession, s.id).status == "active"


def test_the_first_end_reason_is_kept_when_a_failed_session_is_destroyed(db, published, session_factory):
    """Regression (found through the gateway): a later 'disconnect' used to erase 'limit exceeded' as the cause."""
    mgr, _ = make(session_factory)
    s = mgr.create_session()
    mgr.execute(s.id, "saturate")
    done = mgr.destroy_session(s.id, "disconnect")
    assert done.status == "destroyed" and done.end_reason == "limit exceeded: process limit"


def test_unknown_session(db, published, session_factory):
    mgr, _ = make(session_factory)
    with pytest.raises(sm.SessionNotFound):
        mgr.execute(uuid.uuid4(), "echo hi")
    with pytest.raises(sm.SessionNotFound):
        mgr.destroy_session(uuid.uuid4())


def test_reap_idle_and_ttl(db, published, session_factory):
    mgr, driver = make(session_factory, idle_ttl_s=60, session_ttl_s=3600)
    s = mgr.create_session()
    assert mgr.reap() == []
    later = datetime.now(UTC) + timedelta(seconds=120)
    assert mgr.reap(later) == [s.id]
    assert s.runtime_id in driver.destroyed
    s2 = mgr.create_session()
    assert mgr.reap(datetime.now(UTC) + timedelta(hours=2)) == [s2.id]


def test_reconcile_removes_orphans_and_fails_stranded(db, published, session_factory):
    mgr, driver = make(session_factory)
    s = mgr.create_session()
    driver.create(None, None)  # an orphan runtime nobody tracks
    fresh = sm.SessionManager(driver, session_factory, mgr.policy, "hornet-twin-base:2")  # "restarted"
    assert fresh.reconcile() == 2
    assert driver.runtimes == {}
    with session_factory() as v:
        assert v.get(HoneypotSession, s.id).status == "failed"


def test_image_digest_mismatch_refused(db, session_factory):
    snap = services.create_draft_snapshot(db, None)
    db.add(TwinImage(snapshot_id=snap.id, image_digest="sha256:aaaa", runtime_type="docker"))
    services.validate_snapshot(db, snap)
    services.publish_snapshot(db, snap)  # the image is bound to the snapshot before it is locked
    db.commit()
    mgr, driver = make(session_factory)
    driver.images["sha256:aaaa"] = "sha256:bbbb"
    with pytest.raises(ImageMismatch):
        mgr.create_session()
    assert driver.runtimes == {}  # nothing launched
    with session_factory() as v:  # the refusal is recorded
        assert v.scalars(select(HoneypotSession)).one().status == "failed"


def test_illegal_transitions():
    lifecycle.check("requested", "provisioning")
    for cur, new in [("destroyed", "active"), ("ready", "destroyed"), ("failed", "active"), ("active", "ready")]:
        with pytest.raises(lifecycle.IllegalTransition):
            lifecycle.check(cur, new)


@pytest.mark.parametrize(
    "bad",
    [{"timeout_ms": 10**7}, {"memory_mb": 10**5}, {"pids": 100000}, {"cpus": 64}, {"output_limit_bytes": 10**9}],
)
def test_policy_bounds(bad):
    with pytest.raises(ValueError):
        ExecutionPolicy(**bad)
