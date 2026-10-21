"""Shared setup for AI synthesis tests: a published snapshot with a database schema and three fake services."""
import uuid
from datetime import UTC, datetime

from app.ai.synthesis import SynthesisWorker
from app.models import (
    EnvironmentSnapshot,
    ExecutionRequest,
    HoneypotSession,
    ServiceProfile,
    SnapshotStatus,
    VirtualNode,
)

SCHEMA = {"customers": ["id", "email", "full_name", "status"], "orders": ["id", "customer_id", "total", "created_at"]}


def published_snapshot(db) -> EnvironmentSnapshot:
    snap = EnvironmentSnapshot(version=1, status=SnapshotStatus.draft.value, profile_hash="test")
    db.add(snap)
    db.flush()
    root = VirtualNode(snapshot_id=snap.id, virtual_path="/", node_type="directory")
    dbs = VirtualNode(snapshot_id=snap.id, virtual_path="/databases", node_type="directory", parent_id=None)
    db.add_all([root, dbs])
    db.flush()
    dbs.parent_id = root.id
    db.add(VirtualNode(snapshot_id=snap.id, virtual_path="/databases/shop", node_type="database", parent_id=dbs.id,
                       meta={"engine": "postgres", "tables": {"customers": 3, "orders": 2}, "columns": SCHEMA}))
    for name, loop in (("db", "127.0.1.10"), ("cache", "127.0.1.11"), ("web", "127.0.1.12")):
        db.add(ServiceProfile(snapshot_id=snap.id, service_type=name,
                              behavior_config={"name": name, "loopback": loop}))
    db.flush()
    snap.status = SnapshotStatus.published.value
    snap.published_at = datetime.now(UTC)
    db.commit()
    return snap


def session_and_request(db, snap) -> tuple[uuid.UUID, uuid.UUID]:
    sess = HoneypotSession(snapshot_id=snap.id, status="active")
    db.add(sess)
    db.flush()
    req = ExecutionRequest(session_id=sess.id, operation="command", input_hash="0" * 64, input_text="psql ...")
    db.add(req)
    db.commit()
    return sess.id, req.id


def worker(provider, session_factory, timeout_s: float = 1.0, max_per_session: int = 20) -> SynthesisWorker:
    return SynthesisWorker(provider, session_factory, timeout_s=timeout_s, max_per_session=max_per_session)


def pg_event(sql: str) -> dict:
    return {"ts": "2026-10-19T00:00:00.000+00:00", "service": "db", "proto": "postgres", "action": "sql", "sql": sql,
            "status": "error", "code": "42601", "unsupported": True}


def redis_event(command: str) -> dict:
    return {"ts": "2026-10-19T00:00:00.000+00:00", "service": "cache", "proto": "redis", "action": command,
            "status": "error", "unsupported": True}


def http_event(action: str, body: str = "") -> dict:
    return {"ts": "2026-10-19T00:00:00.000+00:00", "service": "web", "proto": "http", "action": action,
            "status": 404, "body": body, "unsupported": True}
