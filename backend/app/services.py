"""Domain logic for snapshots and audit events (kept out of the API layer)."""
import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.graph import NodeSpec, normalize_path, validate_tree
from app.models import (
    AdminAuditEvent,
    EnvironmentSnapshot,
    NodeType,
    SnapshotStatus,
    VirtualNode,
)

# Minimal placeholder tree until the Phase 3 builder generates real ones.
DEFAULT_TREE = [
    ("/", None, "directory"),
    ("/etc", "/", "directory"),
    ("/var", "/", "directory"),
    ("/var/www", "/var", "directory"),
    ("/home", "/", "directory"),
]


class ConflictError(Exception):
    pass


def audit(db: Session, actor: str, action: str, target: str | None = None, **detail) -> None:
    db.add(AdminAuditEvent(actor=actor, action=action, target=target, detail=detail))


def next_version(db: Session) -> int:
    return (db.scalar(select(func.max(EnvironmentSnapshot.version))) or 0) + 1


def create_draft_snapshot(db: Session, nodes: list[tuple[str, str | None, str]] | None) -> EnvironmentSnapshot:
    """nodes: (path, parent_path, node_type). Paths are normalized; invalid paths raise PathError."""
    spec = [(normalize_path(p), normalize_path(pp) if pp else None, t) for p, pp, t in (nodes or DEFAULT_TREE)]
    snap = EnvironmentSnapshot(
        version=next_version(db),
        status=SnapshotStatus.draft.value,
        profile_hash=hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
    )
    db.add(snap)
    db.flush()
    ids = {}
    for path, parent, ntype in sorted(spec, key=lambda s: s[0].count("/")):
        node = VirtualNode(
            snapshot_id=snap.id,
            parent_id=ids.get(parent),
            virtual_path=path,
            node_type=NodeType(ntype).value,
        )
        db.add(node)
        db.flush()
        ids[path] = node.id
    return snap


def validate_snapshot(db: Session, snap: EnvironmentSnapshot) -> dict:
    if snap.status not in (SnapshotStatus.draft.value, SnapshotStatus.validated.value):
        raise ConflictError(f"cannot validate a {snap.status} snapshot")
    nodes = db.scalars(select(VirtualNode).where(VirtualNode.snapshot_id == snap.id)).all()
    by_id = {n.id: n for n in nodes}
    specs = [
        NodeSpec(
            key=str(n.id),
            parent_key=str(n.parent_id) if n.parent_id else None,
            path=n.virtual_path,
            node_type=n.node_type,
        )
        for n in nodes
    ]
    errors = validate_tree(specs) if by_id else ["snapshot has no nodes"]
    report = {"ok": not errors, "errors": errors, "node_count": len(nodes)}
    build = (snap.validation_report or {}).get("build")  # keep the builder's report if there is one
    snap.validation_report = {**report, "build": build} if build else report
    snap.status = SnapshotStatus.validated.value if not errors else SnapshotStatus.draft.value
    return report


def publish_snapshot(db: Session, snap: EnvironmentSnapshot) -> EnvironmentSnapshot:
    """Atomic: archive the previous published snapshot and publish this validated one."""
    if snap.status != SnapshotStatus.validated.value:
        raise ConflictError("only a validated snapshot can be published")
    previous = db.scalars(
        select(EnvironmentSnapshot).where(EnvironmentSnapshot.status == SnapshotStatus.published.value)
    ).all()
    for old in previous:
        old.status = SnapshotStatus.archived.value
    snap.status = SnapshotStatus.published.value
    snap.published_at = datetime.now(UTC)
    return snap
