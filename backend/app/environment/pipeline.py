"""The build pipeline (README section 4.2):

  discover (read-only) -> sanitize -> plan -> validate -> write context -> build image -> record snapshot

A build either produces a complete, validated *draft* snapshot or nothing. It never touches the
currently published snapshot; publishing stays an explicit admin action.
"""
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app import services
from app.ai.provider import AIProvider
from app.discovery.docker_inventory import DiscoveryError, ReadOnlyDocker, collect
from app.environment.builder import build_plan
from app.environment.graph_nodes import plan_nodes
from app.environment.image_builder import ImageBuilder, ImageBuildError, write_context
from app.environment.plan import EnvPlan
from app.environment.validator import validate_plan
from app.models import (
    EnvironmentSnapshot,
    NodeType,
    ServiceProfile,
    SnapshotStatus,
    TwinImage,
    VirtualNode,
)
from app.profile import SanitizedProfile
from app.sanitization.ai_review import review_profile
from app.sanitization.sanitizer import SanitizationFailure, assert_clean, sanitize


class BuildFailed(Exception):
    def __init__(self, stage: str, message: str, report: dict | None = None):
        super().__init__(message)
        self.stage, self.report = stage, report or {}


def discover_and_sanitize(docker: ReadOnlyDocker, project: str, allowed: set[str],
                          forbidden: tuple[str, ...] = (), reviewer: AIProvider | None = None,
                          sanitizer_mode: str = "off") -> SanitizedProfile:
    """Discovery -> deterministic sanitizer -> (optional) AI review -> final leak check."""
    raw = collect(docker, project, allowed)
    try:
        profile = sanitize(raw)
        if reviewer is not None and getattr(reviewer, "is_model", False) and sanitizer_mode != "off":
            profile = review_profile(profile, reviewer, sanitizer_mode)
    except SanitizationFailure as exc:
        raise BuildFailed("sanitization", str(exc)) from exc
    try:
        assert_clean(profile, forbidden)
    except SanitizationFailure as exc:
        raise BuildFailed("sanitization", str(exc)) from exc
    return profile


def _materialize(db: Session, plan: EnvPlan, profile: SanitizedProfile, image_id: str, report: dict) -> EnvironmentSnapshot:
    snap = EnvironmentSnapshot(
        version=services.next_version(db),
        status=SnapshotStatus.draft.value,
        profile_hash=profile.profile_hash,
        validation_report={"build": report},
    )
    db.add(snap)
    db.flush()

    ids: dict[str, uuid.UUID] = {}
    for n in sorted(plan_nodes(plan), key=lambda n: (n.path.count("/") if n.path != "/" else 0, n.path)):
        node_id = uuid.uuid4()
        ids[n.path] = node_id
        db.add(VirtualNode(
            id=node_id, snapshot_id=snap.id, parent_id=ids.get(n.parent_path) if n.parent_path else None,
            virtual_path=n.path, node_type=NodeType(n.node_type).value, permissions=n.permissions,
            content_ref=n.content_ref, sensitivity_class=n.sensitivity, meta=n.meta,
        ))
    for sp in plan.services:
        db.add(ServiceProfile(snapshot_id=snap.id, service_type=sp.role, behavior_config={
            "name": sp.name, "image": sp.image, "ports": sp.ports, "ip": sp.ip, "user": sp.user, "loopback": sp.loopback, "emulator": sp.emulator,
            "fixtures": "services.json",
        }))
    db.add(TwinImage(snapshot_id=snap.id, image_digest=image_id, runtime_type="docker"))
    db.flush()
    return snap


def build_from_docker(
    db: Session,
    *,
    docker: ReadOnlyDocker,
    image_builder: ImageBuilder,
    provider: AIProvider | None,
    project: str,
    allowed_projects: set[str],
    base_image: str,
    build_dir: Path,
    actor: str,
    now: datetime | None = None,
    forbidden: tuple[str, ...] = (),
    sanitizer_mode: str = "off",
) -> EnvironmentSnapshot:
    now = now or datetime.now(UTC)
    report: dict = {"source": "docker", "project": project, "stages": []}
    image_id: str | None = None
    try:
        profile = discover_and_sanitize(docker, project, allowed_projects, forbidden, provider, sanitizer_mode)
        report["stages"].append("sanitized")
        report["sanitization"] = profile.report.model_dump()
        report["profile"] = profile.model_dump(exclude={"report"})

        plan, ai_report = build_plan(profile, now, provider, forbidden)
        report["ai"] = ai_report.as_dict() if ai_report else {"requested": False}
        report["data_model"] = plan.notes.get("data_model", {})
        report["stages"].append("planned")

        check = validate_plan(plan, forbidden)
        report["validation"] = check.as_dict()
        if not check.ok:
            raise BuildFailed("validation", f"{len(check.errors)} validation error(s)", report)
        report["stages"].append("validated")

        ctx_dir = build_dir / profile.profile_hash[:12]
        report["context_sha256"] = write_context(plan, ctx_dir, base_image, profile.profile_hash)
        try:
            image_id = image_builder.build(ctx_dir, f"hornet-twin:{profile.profile_hash[:12]}")
        except ImageBuildError as exc:
            raise BuildFailed("image", str(exc), report) from exc
        report["image"] = {"digest": image_id, "base": base_image}
        report["stages"].append("image_built")

        snap = _materialize(db, plan, profile, image_id, report)
        result = services.validate_snapshot(db, snap)
        if not result["ok"]:
            raise BuildFailed("snapshot", "graph invariants failed: " + "; ".join(result["errors"][:3]), report)
        snap.validation_report = {**result, "build": report}
        services.audit(db, actor, "environment.build", str(snap.id), source="docker", project=project,
                       version=snap.version, image=image_id, findings=profile.report.findings_by_kind)
        db.commit()
        return snap
    except BuildFailed as exc:
        _abort(db, image_builder, image_id, actor, project, exc)
        raise
    except DiscoveryError as exc:  # keep the type: the API maps it to 403/422/503
        _abort(db, image_builder, image_id, actor, project, BuildFailed("discovery", type(exc).__name__))
        raise
    except Exception as exc:
        failure = BuildFailed("internal", type(exc).__name__, report)
        _abort(db, image_builder, image_id, actor, project, failure)
        raise failure from exc


def _abort(db: Session, image_builder: ImageBuilder, image_id: str | None, actor: str, project: str,
           exc: BuildFailed) -> None:
    """Roll back, drop any half-built image, and leave an audit trail. The published snapshot is untouched."""
    db.rollback()
    if image_id:
        image_builder.remove(image_id)
    services.audit(db, actor, "environment.build_failed", project, stage=exc.stage, reason=str(exc)[:200])
    if exc.stage == "sanitization":  # worth a louder trail: something sensitive nearly got through
        services.audit(db, actor, "security.sanitization_failed", project, reason=str(exc)[:200])
    db.commit()
