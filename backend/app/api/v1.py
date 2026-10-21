import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import schemas, services
from app.ai.nim_provider import NimProvider
from app.ai.provider import TemplateProvider
from app.api.auth import require_admin
from app.api.live import router as live_router
from app.config import Settings, get_settings
from app.db import get_db, get_sessionmaker
from app.discovery.docker_inventory import (
    DiscoveryError,
    DiscoveryNotAllowed,
    DiscoveryUnavailable,
    ReadOnlyDocker,
)
from app.environment import pipeline
from app.environment.image_builder import DockerImageBuilder
from app.graph import PathError
from app.models import (
    AdminAuditEvent,
    AiSynthesis,
    EnvironmentSnapshot,
    ExecutionObservation,
    ExecutionRequest,
    GatewayEvent,
    HoneypotSession,
    SessionEvent,
    SessionOverlay,
    SessionStatus,
    SnapshotStatus,
    VirtualNode,
)
from app.orchestration import session_manager as sm
from app.runtime.driver import ImageMismatch, RuntimeUnavailable
from app.runtime.factory import ensure_manager

_manager_lock = threading.Lock()

_SESSION_ERRORS = {
    sm.SnapshotNotPublished: 409,
    sm.SessionLimitReached: 429,
    sm.SessionNotFound: 404,
    sm.SessionNotUsable: 409,
    sm.InputRejected: 422,
    ImageMismatch: 409,
    RuntimeUnavailable: 503,
}


def get_manager(request: Request, settings: Settings = Depends(get_settings)) -> sm.SessionManager:
    """Lazily create the orchestrator so the API still starts when Docker is unavailable."""
    try:
        return ensure_manager(request.app.state, settings, get_sessionmaker())
    except RuntimeUnavailable:
        raise HTTPException(503, "twin runtime unavailable") from None


def get_docker_services():
    """Returns a factory so Docker is only contacted for builds/previews that actually need it."""

    def make() -> tuple[ReadOnlyDocker, DockerImageBuilder]:
        import docker

        try:
            client = docker.from_env()
            client.ping()
            return ReadOnlyDocker(client), DockerImageBuilder(client)
        except Exception:
            raise HTTPException(503, "docker unavailable") from None

    return make


def get_ai_provider(settings: Settings = Depends(get_settings)):
    """NVIDIA NIM when configured (HORNET_AI_PROVIDER=nim + NVIDIA_API_KEY), otherwise the offline template."""
    if settings.ai_provider == "nim":
        return NimProvider(
            settings.nvidia_api_key.get_secret_value(), settings.nim_model, settings.nim_base_url,
            settings.nim_timeout_s, settings.nim_max_tokens,
        )
    return TemplateProvider()


def _http(exc: Exception) -> HTTPException:
    for etype, code in _SESSION_ERRORS.items():
        if isinstance(exc, etype):
            return HTTPException(code, str(exc))
    raise exc


public = APIRouter()
admin = APIRouter(dependencies=[Depends(require_admin)])


def _page(settings: Settings, limit: int | None, offset: int) -> tuple[int, int]:
    limit = min(limit or settings.page_size_default, settings.page_size_max)
    return limit, offset


def _snapshot_or_404(db: Session, snapshot_id: uuid.UUID) -> EnvironmentSnapshot:
    snap = db.get(EnvironmentSnapshot, snapshot_id)
    if snap is None:
        raise HTTPException(404, "snapshot not found")
    return snap


# ---- health (unauthenticated, no sensitive data) -------------------------------------------
@public.get("/health")
def health(request: Request, db: Session = Depends(get_db)) -> dict:
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(503, "database unavailable") from None
    mgr = getattr(request.app.state, "manager", None)
    runtime = "not started" if mgr is None else ("up" if mgr.driver.ping() else "down")
    gw = getattr(request.app.state, "gateway", None)
    return {"status": "ok", "database": "up", "runtime": runtime,
            "gateway": f"listening:{gw.port}" if gw is not None else "off"}


# ---- environments --------------------------------------------------------------------------
@admin.post("/environments/build", response_model=schemas.SnapshotOut, status_code=201)
def build_environment(
    body: schemas.BuildRequest | None = None,
    db: Session = Depends(get_db),
    actor: str = Depends(require_admin),
    settings: Settings = Depends(get_settings),
    docker_services=Depends(get_docker_services),
    provider=Depends(get_ai_provider),
):
    """source=seed: a hand-written tree (tests, demos). source=docker: discover, sanitize, generate,
    validate and build a twin image from an allowlisted compose project. Never publishes."""
    body = body or schemas.BuildRequest()
    if body.source == "docker":
        if not body.project:
            raise HTTPException(422, "project is required when source=docker")
        docker, image_builder = docker_services()
        try:
            return pipeline.build_from_docker(
                db, docker=docker, image_builder=image_builder, provider=provider, project=body.project,
                allowed_projects=settings.allowed_projects(), base_image=settings.twin_base_image,
                build_dir=Path(settings.build_dir), actor=actor, sanitizer_mode=settings.sanitizer_mode,
            )
        except DiscoveryNotAllowed as exc:
            raise HTTPException(403, str(exc)) from None
        except DiscoveryUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        except DiscoveryError as exc:
            raise HTTPException(422, str(exc)) from None
        except pipeline.BuildFailed as exc:
            raise HTTPException(422, {"stage": exc.stage, "message": str(exc),
                                      "errors": (exc.report.get("validation") or {}).get("errors", [])[:10]}) from None

    nodes = [(n.path, n.parent_path, n.node_type) for n in body.nodes] if body.nodes else None
    try:
        snap = services.create_draft_snapshot(db, nodes)
    except (PathError, ValueError) as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from None
    services.audit(db, actor, "environment.build", str(snap.id), version=snap.version, source="seed")
    db.commit()
    return snap


@admin.post("/discovery/preview")
def discovery_preview(
    body: schemas.DiscoveryPreviewIn,
    settings: Settings = Depends(get_settings),
    docker_services=Depends(get_docker_services),
    provider=Depends(get_ai_provider),
    db: Session = Depends(get_db),
    actor: str = Depends(require_admin),
):
    """Run discovery + sanitization only and return the sanitized profile for review. Builds nothing."""
    docker, _ = docker_services()
    try:
        profile = pipeline.discover_and_sanitize(
            docker, body.project, settings.allowed_projects(), reviewer=provider, sanitizer_mode=settings.sanitizer_mode
        )
    except DiscoveryNotAllowed as exc:
        raise HTTPException(403, str(exc)) from None
    except DiscoveryUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except DiscoveryError as exc:
        raise HTTPException(422, str(exc)) from None
    except pipeline.BuildFailed as exc:
        services.audit(db, actor, "security.sanitization_failed", body.project, reason=str(exc)[:200])
        db.commit()
        raise HTTPException(422, {"stage": exc.stage, "message": str(exc)}) from None
    services.audit(db, actor, "discovery.preview", body.project, services=len(profile.services))
    db.commit()
    return profile.model_dump()


@admin.get("/environments", response_model=list[schemas.SnapshotOut])
def list_environments(db: Session = Depends(get_db)):
    return db.scalars(select(EnvironmentSnapshot).order_by(EnvironmentSnapshot.version.desc())).all()


@admin.get("/environments/{snapshot_id}", response_model=schemas.SnapshotOut)
def get_environment(snapshot_id: uuid.UUID, db: Session = Depends(get_db)):
    return _snapshot_or_404(db, snapshot_id)


@admin.post("/environments/{snapshot_id}/validate", response_model=schemas.SnapshotOut)
def validate_environment(
    snapshot_id: uuid.UUID, db: Session = Depends(get_db), actor: str = Depends(require_admin)
):
    snap = _snapshot_or_404(db, snapshot_id)
    try:
        report = services.validate_snapshot(db, snap)
    except services.ConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    services.audit(db, actor, "environment.validate", str(snap.id), ok=report["ok"])
    db.commit()
    return snap


@admin.post("/environments/{snapshot_id}/publish", response_model=schemas.SnapshotOut)
def publish_environment(
    snapshot_id: uuid.UUID, db: Session = Depends(get_db), actor: str = Depends(require_admin)
):
    snap = _snapshot_or_404(db, snapshot_id)
    try:
        services.publish_snapshot(db, snap)
    except services.ConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    services.audit(db, actor, "environment.publish", str(snap.id), version=snap.version)
    db.commit()
    return snap


@admin.get("/environments/{snapshot_id}/tree", response_model=schemas.TreeOut)
def environment_tree(snapshot_id: uuid.UUID, db: Session = Depends(get_db)):
    snap = _snapshot_or_404(db, snapshot_id)
    nodes = db.scalars(
        select(VirtualNode).where(VirtualNode.snapshot_id == snap.id).order_by(VirtualNode.virtual_path)
    ).all()
    return schemas.TreeOut(
        snapshot_id=snap.id,
        version=snap.version,
        nodes=[
            schemas.TreeNodeOut(id=n.id, parent_id=n.parent_id, path=n.virtual_path, type=n.node_type)
            for n in nodes
        ],
    )


# ---- sessions ------------------------------------------------------------------------------
@admin.get("/sessions", response_model=schemas.Page[schemas.SessionOut])
def list_sessions(
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    limit, offset = _page(settings, limit, offset)
    total = db.scalar(select(func.count()).select_from(HoneypotSession)) or 0
    rows = db.scalars(
        select(HoneypotSession).order_by(HoneypotSession.started_at.desc()).limit(limit).offset(offset)
    ).all()
    return schemas.Page(items=_with_counts(db, rows), total=total, limit=limit, offset=offset)


def _with_counts(db: Session, sessions) -> list[schemas.SessionOut]:
    """Sessions plus how many commands each ran and how many AI proposals (gaps) they produced. Counts only."""
    ids = [s.id for s in sessions]
    commands = dict(db.execute(select(ExecutionRequest.session_id, func.count()).where(
        ExecutionRequest.session_id.in_(ids)).group_by(ExecutionRequest.session_id)).all()) if ids else {}
    gaps = dict(db.execute(select(ExecutionRequest.session_id, func.count(AiSynthesis.id))
                           .join(AiSynthesis, AiSynthesis.request_id == ExecutionRequest.id)
                           .where(ExecutionRequest.session_id.in_(ids))
                           .group_by(ExecutionRequest.session_id)).all()) if ids else {}
    return [schemas.SessionOut.model_validate(s).model_copy(
        update={"commands": commands.get(s.id, 0), "ai_gaps": gaps.get(s.id, 0)}) for s in sessions]


@admin.get("/sessions/{session_id}", response_model=schemas.SessionOut)
def get_session(session_id: uuid.UUID, db: Session = Depends(get_db)):
    sess = db.get(HoneypotSession, session_id)
    if sess is None:
        raise HTTPException(404, "session not found")
    return _with_counts(db, [sess])[0]


@admin.get("/sessions/{session_id}/events", response_model=schemas.Page[schemas.EventOut])
def session_events(
    session_id: uuid.UUID,
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if db.get(HoneypotSession, session_id) is None:
        raise HTTPException(404, "session not found")
    limit, offset = _page(settings, limit, offset)
    q = select(SessionEvent).where(SessionEvent.session_id == session_id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(SessionEvent.occurred_at).limit(limit).offset(offset)).all()
    items = [
        schemas.EventOut(
            id=e.id, session_id=e.session_id, node_id=e.node_id, event_type=e.event_type,
            metadata=e.meta, occurred_at=e.occurred_at,
        )
        for e in rows
    ]
    return schemas.Page(items=items, total=total, limit=limit, offset=offset)


@admin.get("/sessions/{session_id}/executions", response_model=schemas.Page[schemas.ExecutionOut])
def session_executions(
    session_id: uuid.UUID,
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    actor: str = Depends(require_admin),
):
    """Every command of a session in order: the redacted command, its raw execution evidence and, separately, any
    AI proposals for requests the fake services could not answer. Reads are audited because the output can contain
    whatever the attacker typed."""
    if db.get(HoneypotSession, session_id) is None:
        raise HTTPException(404, "session not found")
    limit, offset = _page(settings, limit, offset)
    q = (select(ExecutionRequest, ExecutionObservation)
         .outerjoin(ExecutionObservation, ExecutionObservation.request_id == ExecutionRequest.id)
         .where(ExecutionRequest.session_id == session_id))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.execute(q.order_by(ExecutionRequest.created_at).limit(limit).offset(offset)).all()
    proposals: dict[uuid.UUID, list[schemas.AiProposalOut]] = {}
    for a in db.scalars(select(AiSynthesis).where(AiSynthesis.request_id.in_([req.id for req, _ in rows]))):
        m = a.response_metadata or {}
        proposals.setdefault(a.request_id, []).append(schemas.AiProposalOut(
            id=a.id, served=bool(m.get("served", False)), status=a.validation_status, reason=m.get("reason", ""),
            service=m.get("service"), proto=m.get("proto"), request=m.get("request"), proposal=m.get("proposal"),
            provider=m.get("provider"), policy_version=m.get("policy_version"), latency_ms=m.get("latency_ms"),
            cache=bool(m.get("cache")),
        ))
    items = []
    for req, obs in rows:
        meta = (obs.result_metadata if obs else None) or {}
        items.append(schemas.ExecutionOut(
            request_id=req.id, operation=req.operation, command=req.input_text, command_raw=req.input_raw,
            input_hash=req.input_hash, created_at=req.created_at, exit_code=obs.exit_code if obs else None,
            stdout=meta.get("stdout", ""), stderr=meta.get("stderr", ""), timed_out=bool(meta.get("timed_out")),
            truncated=bool(meta.get("truncated")), duration_ms=meta.get("duration_ms"),
            runtime_killed=bool(meta.get("runtime_killed")), ai_proposals=proposals.get(req.id, []),
        ))
    services.audit(db, actor, "session.executions.read", str(session_id), limit=limit, offset=offset)
    db.commit()
    return schemas.Page(items=items, total=total, limit=limit, offset=offset)


@admin.get("/sessions/{session_id}/overlay", response_model=schemas.Page[schemas.OverlayOut])
def session_overlay(
    session_id: uuid.UUID,
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """The session's filesystem changes (its overlay on top of the immutable base snapshot)."""
    if db.get(HoneypotSession, session_id) is None:
        raise HTTPException(404, "session not found")
    limit, offset = _page(settings, limit, offset)
    q = select(SessionOverlay).where(SessionOverlay.session_id == session_id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(SessionOverlay.virtual_path).limit(limit).offset(offset)).all()
    items = [schemas.OverlayOut(id=o.id, path=o.virtual_path, operation=o.operation, kind=o.meta.get("kind"),
                                size=o.meta.get("size"), request_id=o.meta.get("request_id")) for o in rows]
    return schemas.Page(items=items, total=total, limit=limit, offset=offset)


@admin.get("/sessions/{session_id}/tree-state", response_model=schemas.TreeStateOut)
def session_tree_state(session_id: uuid.UUID, db: Session = Depends(get_db)):
    """The snapshot's tree as this session saw it: base nodes, visit markers (from events linked to nodes; an
    observation, not proof of access) and overlay markers (the session's file changes, including new files)."""
    sess = db.get(HoneypotSession, session_id)
    if sess is None:
        raise HTTPException(404, "session not found")
    snap = db.get(EnvironmentSnapshot, sess.snapshot_id)
    base = db.scalars(select(VirtualNode).where(VirtualNode.snapshot_id == sess.snapshot_id)
                      .order_by(VirtualNode.virtual_path)).all()
    visits: dict[uuid.UUID, int] = {}
    basis: dict[uuid.UUID, set[str]] = {}
    for ev in db.scalars(select(SessionEvent).where(SessionEvent.session_id == session_id,
                                                    SessionEvent.node_id.is_not(None))):
        visits[ev.node_id] = visits.get(ev.node_id, 0) + 1
        basis.setdefault(ev.node_id, set()).add(
            ev.meta.get("basis") or ("service-request" if ev.event_type.startswith("service.") else ev.event_type))
    # latest operation per path, in command order (overlay rows carry the request that caused them)
    order = {str(r.id): r.created_at for r in db.scalars(select(ExecutionRequest)
                                                          .where(ExecutionRequest.session_id == session_id))}
    changes = sorted(db.scalars(select(SessionOverlay).where(SessionOverlay.session_id == session_id)),
                     key=lambda o: (order.get(o.meta.get("request_id")) is None, order.get(o.meta.get("request_id")) or 0))
    overlay = {o.virtual_path: (o.operation, o.meta.get("kind")) for o in changes}
    nodes = [schemas.TreeStateNodeOut(
        id=n.id, parent_id=n.parent_id, path=n.virtual_path, type=n.node_type, in_base=True,
        sensitivity=n.sensitivity_class,
        visits=visits.get(n.id, 0), basis=sorted(basis.get(n.id, ())),
        overlay=overlay[n.virtual_path][0] if n.virtual_path in overlay else None,
    ) for n in base]
    known = {n.virtual_path for n in base}
    for path, (op, kind) in sorted(overlay.items()):
        if path not in known:
            nodes.append(schemas.TreeStateNodeOut(id=None, parent_id=None, path=path, sensitivity="attacker-created",
                                                  type="directory" if kind == "d" else "file", in_base=False,
                                                  visits=0, basis=[], overlay=op))
    return schemas.TreeStateOut(session_id=session_id, snapshot_id=sess.snapshot_id, version=snap.version, nodes=nodes)


# ---- twin sessions (create / exec / destroy) -----------------------------------------------
@admin.post("/sessions", response_model=schemas.SessionOut, status_code=201)
def create_session(
    body: schemas.CreateSessionIn | None = None,
    mgr: sm.SessionManager = Depends(get_manager),
    actor: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        sess = mgr.create_session(body.snapshot_id if body else None)
    except tuple(_SESSION_ERRORS) as exc:
        raise _http(exc) from None
    services.audit(db, actor, "session.create", str(sess.id))
    db.commit()
    return sess


@admin.post("/sessions/{session_id}/exec", response_model=schemas.ExecOut)
def exec_in_session(
    session_id: uuid.UUID, body: schemas.ExecIn, mgr: sm.SessionManager = Depends(get_manager)
):
    """Admin console into a twin (testing). Attackers reach twins only through the gateway (Phase 4)."""
    try:
        out = mgr.execute(session_id, body.command)
    except tuple(_SESSION_ERRORS) as exc:
        raise _http(exc) from None
    return schemas.ExecOut(**{**out.__dict__, "fs_changes": [schemas.FsChangeOut(**c) for c in out.fs_changes]})


@admin.delete("/sessions/{session_id}", response_model=schemas.SessionOut)
def destroy_session(
    session_id: uuid.UUID,
    mgr: sm.SessionManager = Depends(get_manager),
    actor: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        sess = mgr.destroy_session(session_id, "admin request")
    except tuple(_SESSION_ERRORS) as exc:
        raise _http(exc) from None
    services.audit(db, actor, "session.destroy", str(session_id))
    db.commit()
    return sess


# ---- attacker-facing gateway: connection and login events ---------------------------------------------------
@admin.get("/gateway/events", response_model=schemas.Page[schemas.GatewayEventOut])
def gateway_events(
    event_type: str | None = Query(default=None, max_length=48),
    source_ip: str | None = Query(default=None, max_length=64),
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    limit, offset = _page(settings, limit, offset)
    q = select(GatewayEvent)
    if event_type:
        q = q.where(GatewayEvent.event_type == event_type)
    if source_ip:
        q = q.where(GatewayEvent.source_ip == source_ip)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(GatewayEvent.occurred_at.desc()).limit(limit).offset(offset)).all()
    items = [schemas.GatewayEventOut(id=e.id, connection_id=e.connection_id, session_id=e.session_id, source_ip=e.source_ip,
                                     event_type=e.event_type, metadata=e.meta, occurred_at=e.occurred_at) for e in rows]
    return schemas.Page(items=items, total=total, limit=limit, offset=offset)


# ---- analytics and audit -------------------------------------------------------------------
@admin.get("/analytics/overview", response_model=schemas.OverviewOut)
def overview(db: Session = Depends(get_db)):
    snaps = dict(db.execute(select(EnvironmentSnapshot.status, func.count()).group_by(EnvironmentSnapshot.status)).all())
    active_states = [SessionStatus.provisioning.value, SessionStatus.ready.value, SessionStatus.active.value]
    return schemas.OverviewOut(
        snapshots=snaps,
        sessions_total=db.scalar(select(func.count()).select_from(HoneypotSession)) or 0,
        sessions_active=db.scalar(
            select(func.count()).select_from(HoneypotSession).where(HoneypotSession.status.in_(active_states))
        ) or 0,
        events_total=db.scalar(select(func.count()).select_from(SessionEvent)) or 0,
        events_by_type=dict(db.execute(select(SessionEvent.event_type, func.count()).group_by(SessionEvent.event_type)).all()),
        published_version=db.scalar(
            select(EnvironmentSnapshot.version).where(EnvironmentSnapshot.status == SnapshotStatus.published.value)
        ),
    )


@admin.get("/audit", response_model=schemas.Page[schemas.AuditOut])
def audit_log(
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    limit, offset = _page(settings, limit, offset)
    total = db.scalar(select(func.count()).select_from(AdminAuditEvent)) or 0
    items = db.scalars(
        select(AdminAuditEvent).order_by(AdminAuditEvent.occurred_at.desc()).limit(limit).offset(offset)
    ).all()
    return schemas.Page(items=items, total=total, limit=limit, offset=offset)


router = APIRouter(prefix="/api/v1")
router.include_router(public)
router.include_router(admin)
router.include_router(live_router)  # authenticates in its first message (see app/api/live.py)
