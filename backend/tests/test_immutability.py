import pytest

from app import services
from app.immutability import ImmutableError
from app.models import (
    AdminAuditEvent,
    HoneypotSession,
    SessionEvent,
    SnapshotStatus,
    VirtualNode,
)


def _published(db):
    snap = services.create_draft_snapshot(db, None)
    services.validate_snapshot(db, snap)
    services.publish_snapshot(db, snap)
    db.commit()
    return snap


def test_publish_requires_validation(db):
    snap = services.create_draft_snapshot(db, None)
    db.commit()
    with pytest.raises(services.ConflictError):
        services.publish_snapshot(db, snap)


def test_published_snapshot_cannot_change(db):
    snap = _published(db)
    snap.profile_hash = "tampered"
    with pytest.raises(ImmutableError):
        db.commit()
    db.rollback()


def test_published_snapshot_nodes_cannot_change_add_or_delete(db):
    snap = _published(db)
    node = db.query(VirtualNode).filter_by(snapshot_id=snap.id).first()

    node.virtual_path = "/evil"
    with pytest.raises(ImmutableError):
        db.commit()
    db.rollback()

    db.add(VirtualNode(snapshot_id=snap.id, virtual_path="/new", node_type="file"))
    with pytest.raises(ImmutableError):
        db.commit()
    db.rollback()

    db.delete(db.query(VirtualNode).filter_by(snapshot_id=snap.id).first())
    with pytest.raises(ImmutableError):
        db.commit()
    db.rollback()


def test_published_snapshot_cannot_be_deleted(db):
    snap = _published(db)
    db.delete(snap)
    with pytest.raises(ImmutableError):
        db.commit()
    db.rollback()


def test_new_publish_archives_previous_and_stays_immutable(db):
    first = _published(db)
    second = services.create_draft_snapshot(db, None)
    services.validate_snapshot(db, second)
    services.publish_snapshot(db, second)
    db.commit()
    assert first.status == SnapshotStatus.archived.value
    assert second.status == SnapshotStatus.published.value
    # archived stays locked, and cannot be re-published or edited
    first.status = SnapshotStatus.published.value
    with pytest.raises(ImmutableError):
        db.commit()
    db.rollback()


def test_draft_is_editable(db):
    snap = services.create_draft_snapshot(db, None)
    db.commit()
    snap.profile_hash = "ok"
    db.commit()


def test_events_and_audit_are_append_only(db):
    snap = _published(db)
    sess = HoneypotSession(snapshot_id=snap.id)
    db.add(sess)
    db.commit()
    ev = SessionEvent(session_id=sess.id, event_type="command")
    audit = AdminAuditEvent(actor="a", action="x")
    db.add_all([ev, audit])
    db.commit()

    ev.event_type = "changed"
    with pytest.raises(ImmutableError):
        db.commit()
    db.rollback()

    db.delete(audit)
    with pytest.raises(ImmutableError):
        db.commit()
    db.rollback()
