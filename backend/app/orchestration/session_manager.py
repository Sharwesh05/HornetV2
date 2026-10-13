"""Session manager / twin orchestrator.

One session = one published snapshot = one isolated runtime = one filesystem overlay.
Nothing here knows what application the twin imitates: the snapshot decides the image, the
driver decides how isolation is done, and the policy decides every limit.
"""
import hashlib
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    EnvironmentSnapshot,
    ExecutionObservation,
    ExecutionRequest,
    HoneypotSession,
    ServiceProfile,
    SessionEvent,
    SessionOverlay,
    SessionStatus,
    SnapshotStatus,
    TwinImage,
    VirtualNode,
)
from app.orchestration import lifecycle
from app.orchestration.linker import touched_paths
from app.orchestration.shell_state import ShellState
from app.runtime import fsdiff
from app.runtime.driver import (
    FsEntry,
    ImageMismatch,
    RuntimeDriver,
    RuntimeKilled,
    RuntimeSpec,
    RuntimeUnavailable,
)
from app.runtime.policy import ExecutionPolicy

S = SessionStatus


class SessionError(Exception):
    pass


class SessionLimitReached(SessionError):
    pass


class SnapshotNotPublished(SessionError):
    pass


class SessionNotFound(SessionError):
    pass


class SessionNotUsable(SessionError):
    pass


class InputRejected(SessionError):
    pass


@dataclass
class _Live:
    runtime_id: str
    spec: RuntimeSpec
    fs: list[FsEntry]
    lock: threading.Lock = field(default_factory=threading.Lock)
    shell: ShellState = field(default_factory=ShellState)
    has_services: bool = False
    events_offset: int = 0


@dataclass
class ExecOutcome:
    request_id: uuid.UUID
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration_ms: int
    runtime_killed: bool
    fs_changes: list[dict]
    cwd: str = "/home/user"
    service_events: int = 0


def _now() -> datetime:
    return datetime.now(UTC)


class SessionManager:
    def __init__(
        self,
        driver: RuntimeDriver,
        db_factory: sessionmaker[Session],
        policy: ExecutionPolicy,
        base_image: str,
    ):
        self.driver = driver
        self.db_factory = db_factory
        self.policy = policy
        self.base_image = base_image
        self._live: dict[uuid.UUID, _Live] = {}
        self._guard = threading.Lock()
        self._nodes: dict[uuid.UUID, dict[str, uuid.UUID]] = {}  # snapshot -> {virtual path: node id}; snapshots are immutable

    # ---- helpers ---------------------------------------------------------------------------
    def _transition(self, db: Session, sess: HoneypotSession, new: str, reason: str | None = None) -> None:
        lifecycle.check(sess.status, new)
        sess.status = new
        if reason and not sess.end_reason:  # the first cause wins: a later 'disconnect' must not erase 'limit exceeded'
            sess.end_reason = reason
        db.add(SessionEvent(session_id=sess.id, event_type=f"session.{new}", meta={"reason": reason}))

    def _resolve_image(self, db: Session, snap: EnvironmentSnapshot) -> str:
        """Snapshot -> pinned twin image. A recorded digest that does not match is refused."""
        twin = db.scalars(select(TwinImage).where(TwinImage.snapshot_id == snap.id)).first()
        if twin is None:
            return self.base_image  # generic base image until the Phase 3 builder produces one
        actual = self.driver.resolve_image(twin.image_digest)
        if not (actual == twin.image_digest or actual.endswith(twin.image_digest)):
            raise ImageMismatch("recorded image digest does not match the runtime image")
        return twin.image_digest

    def _node_index(self, db: Session, snapshot_id: uuid.UUID) -> dict[str, uuid.UUID]:
        with self._guard:
            cached = self._nodes.get(snapshot_id)
        if cached is None:
            rows = db.execute(select(VirtualNode.virtual_path, VirtualNode.id).where(VirtualNode.snapshot_id == snapshot_id)).all()
            cached = {path: nid for path, nid in rows}
            with self._guard:
                self._nodes[snapshot_id] = cached
        return cached

    def _ingest_service_events(self, db: Session, sess: HoneypotSession, live: _Live) -> int:
        """Pull what the in-twin fake services logged and store it, linked to the service's graph node."""
        if not live.has_services:
            return 0
        events, live.events_offset = self.driver.collect_service_events(live.runtime_id, live.spec, live.events_offset)
        index = self._node_index(db, sess.snapshot_id)
        count = 0
        for ev in events[:200]:
            proto, name = ev.get("proto"), ev.get("service")
            if not proto:
                continue  # lifecycle lines such as 'ready'
            when = None
            try:
                when = datetime.fromisoformat(ev["ts"]) if ev.get("ts") else None
            except (ValueError, TypeError):
                pass
            db.add(SessionEvent(
                session_id=sess.id, node_id=index.get(f"/services/{name}"), event_type=f"service.{proto}",
                meta={k: v for k, v in ev.items() if k not in ("ts", "proto")}, occurred_at=when or _now(),
            ))
            count += 1
        if len(events) > 200:
            db.add(SessionEvent(session_id=sess.id, event_type="service.overflow", meta={"dropped": len(events) - 200}))
        return count

    def configure_shell(self, session_id: uuid.UUID, env: dict[str, str]) -> None:
        """Seed the persistent shell environment (e.g. TERM/COLUMNS/LINES from the SSH client's terminal)."""
        with self._guard:
            live = self._live.get(session_id)
        if live is not None:
            safe = {k: v[:200] for k, v in env.items() if k.replace("_", "").isalnum() and not k[0].isdigit()}
            with live.lock:
                live.shell.env.update(safe)

    def shell_context(self, session_id: uuid.UUID) -> tuple[str, str]:
        """(cwd, hostname) for the prompt."""
        with self._guard:
            live = self._live.get(session_id)
        if live is None:
            raise SessionNotUsable("session is not running")
        return live.shell.cwd, live.spec.hostname

    def active_count(self) -> int:
        with self._guard:
            return len(self._live)

    # ---- lifecycle -------------------------------------------------------------------------
    def create_session(self, snapshot_id: uuid.UUID | None = None, source_ip: str | None = None) -> HoneypotSession:
        with self.db_factory() as db:
            if snapshot_id is None:
                snap = db.scalars(
                    select(EnvironmentSnapshot).where(EnvironmentSnapshot.status == SnapshotStatus.published.value)
                ).first()
            else:
                snap = db.get(EnvironmentSnapshot, snapshot_id)
            if snap is None or snap.status != SnapshotStatus.published.value:
                raise SnapshotNotPublished("sessions can only start on a published snapshot")

            with self._guard:  # reserve a slot before doing slow work
                if len(self._live) >= self.policy.max_active_sessions:
                    raise SessionLimitReached("maximum number of active sessions reached")
                sid = uuid.uuid4()
                self._live[sid] = None  # type: ignore[assignment]  # placeholder reservation

            sess = HoneypotSession(id=sid, snapshot_id=snap.id, source_ip=source_ip, status=S.requested.value)
            db.add(sess)
            db.flush()
            try:
                self._transition(db, sess, S.provisioning)
                image = self._resolve_image(db, snap)
                hosts = {}
                for prof in db.scalars(select(ServiceProfile).where(ServiceProfile.snapshot_id == snap.id)):
                    cfg = prof.behavior_config or {}
                    if cfg.get("loopback") and cfg.get("name"):
                        hosts[cfg["name"]] = cfg["loopback"]
                spec = RuntimeSpec(
                    image=image,
                    session_id=str(sid),
                    hostname=f"srv-{secrets.token_hex(3)}",
                    extra_hosts=hosts,
                )
                runtime_id = self.driver.create(spec, self.policy)
                sess.runtime_id = runtime_id
                services_status = self.driver.start_services(runtime_id, spec, self.policy) if hosts else "none"
                baseline = self.driver.snapshot_fs(runtime_id, spec, self.policy) if self.policy.observe_fs else []
                with self._guard:
                    self._live[sid] = _Live(runtime_id=runtime_id, spec=spec, fs=baseline,
                                            has_services=services_status == "started")
                self._transition(db, sess, S.ready)
                db.add(SessionEvent(session_id=sid, event_type="session.services", meta={"status": services_status, "services": sorted(hosts)}))
                sess.last_activity_at = _now()
                db.commit()
            except Exception as exc:
                db.rollback()
                with self._guard:
                    self._live.pop(sid, None)
                self._record_failure(sid, snap.id, str(exc)[:200], locals().get("runtime_id"))
                raise
            return sess

    def _record_failure(self, sid: uuid.UUID, snapshot_id: uuid.UUID, reason: str, runtime_id: str | None) -> None:
        if runtime_id:
            try:
                self.driver.destroy(runtime_id)
            except Exception:  # noqa: BLE001
                pass
        with self.db_factory() as db:
            sess = HoneypotSession(
                id=sid, snapshot_id=snapshot_id, status=S.failed.value, end_reason=reason[:64], ended_at=_now()
            )
            db.add(sess)
            db.add(SessionEvent(session_id=sid, event_type="session.failed", meta={"reason": reason}))
            db.commit()

    def destroy_session(self, session_id: uuid.UUID, reason: str = "requested") -> HoneypotSession:
        with self._guard:
            live = self._live.pop(session_id, None)
        with self.db_factory() as db:
            sess = db.get(HoneypotSession, session_id)
            if sess is None:
                raise SessionNotFound(str(session_id))
            if sess.status == S.destroyed.value:
                return sess  # idempotent
            if live is not None:
                # wait for an in-flight command, then tear down
                with live.lock:
                    try:
                        self._ingest_service_events(db, sess, live)
                    except Exception:  # noqa: BLE001  (the twin may already be gone)
                        pass
                    self.driver.destroy(live.runtime_id)
            elif sess.runtime_id:
                self.driver.destroy(sess.runtime_id)
            if sess.status in (S.provisioning.value, S.ready.value, S.active.value):
                self._transition(db, sess, S.quiescing, reason)
            self._transition(db, sess, S.destroyed, reason)
            sess.ended_at = _now()
            db.commit()
            return sess

    # ---- execution -------------------------------------------------------------------------
    def execute(self, session_id: uuid.UUID, command: str, operation: str = "command", *,
                stateful: bool = False) -> ExecOutcome:
        """Run one command. stateful=True carries the working directory and exported variables from the
        previous command (a persistent shell as far as the attacker can tell); the evidence still records
        exactly what the attacker typed, as one request per command."""
        raw = command.encode("utf-8", "replace")
        if not raw or len(raw) > self.policy.input_limit_bytes:
            raise InputRejected(f"input must be 1..{self.policy.input_limit_bytes} bytes")
        if "\x00" in command:
            raise InputRejected("NUL bytes are not allowed")

        with self._guard:
            live = self._live.get(session_id)
        if live is None:
            with self.db_factory() as db:
                if db.get(HoneypotSession, session_id) is None:
                    raise SessionNotFound(str(session_id))
            raise SessionNotUsable("session is not running")

        with live.lock:  # one command at a time per session
            with self.db_factory() as db:
                sess = db.get(HoneypotSession, session_id)
                if sess is None or sess.status not in (S.ready.value, S.active.value):
                    raise SessionNotUsable("session is not running")
                if sess.status == S.ready.value:
                    self._transition(db, sess, S.active)

                req = ExecutionRequest(
                    session_id=session_id, operation=operation, input_hash=hashlib.sha256(raw).hexdigest()
                )
                db.add(req)
                db.flush()

                script, nonce = live.shell.wrap(command) if stateful else (command, "")
                cwd_before = live.shell.cwd
                try:
                    result = self.driver.exec(live.runtime_id, live.spec, script, self.policy)
                except RuntimeUnavailable as exc:
                    self._fail(db, sess, live, f"runtime unavailable: {exc}"[:64])
                    raise SessionNotUsable("runtime unavailable") from exc
                if stateful:
                    result.stdout, rc = live.shell.parse(result.stdout, nonce)
                    if rc is not None:
                        result.exit_code = rc

                changes: list[dict] = []
                processes: list[dict] = []
                if result.runtime_killed:
                    self._fail(db, sess, live, "limit exceeded: runtime killed")
                else:
                    # `docker top` runs on the host (no fork inside the twin), so it works even when the twin is wedged
                    processes = self.driver.processes(live.runtime_id)
                    if self._exhausted(result, processes):
                        # e.g. a fork bomb: the host is safe (cgroup limits), but the twin can no longer start
                        # processes, so it is unusable. Treat it as a limit violation, not as a normal session.
                        result.runtime_killed = True
                        self._fail(db, sess, live, "limit exceeded: process limit")
                    elif self.policy.observe_fs:
                        try:
                            after = self.driver.snapshot_fs(live.runtime_id, live.spec, self.policy)
                            changes = fsdiff.diff(live.fs, after)
                            live.fs = after
                        except (RuntimeKilled, RuntimeUnavailable):
                            self._fail(db, sess, live, "observer failed")

                # Raw execution evidence: written by the observer, never by the AI.
                db.add(ExecutionObservation(
                    request_id=req.id,
                    exit_code=result.exit_code,
                    result_metadata={
                        "source": "execution",
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "timed_out": result.timed_out,
                        "truncated": result.truncated,
                        "duration_ms": result.duration_ms,
                        "runtime_killed": result.runtime_killed,
                        "process_count": len(processes),
                    },
                ))
                for ch in changes:
                    db.add(SessionOverlay(
                        session_id=session_id, virtual_path=ch["path"], operation=ch["operation"],
                        meta={"kind": ch["kind"], "size": ch["size"], "request_id": str(req.id)},
                    ))
                # Links to the static graph. Observations, not proof (see orchestration/linker.py).
                index = self._node_index(db, sess.snapshot_id)
                for path in touched_paths(command, cwd_before, live.spec.workdir, set(index)):
                    db.add(SessionEvent(session_id=session_id, node_id=index[path], event_type="resource_access",
                                        meta={"path": path, "basis": "command-argument", "request_id": str(req.id)}))
                for ch in changes[:50]:
                    db.add(SessionEvent(session_id=session_id, node_id=index.get(ch["path"]), event_type="fs_change",
                                        meta={"path": ch["path"], "operation": ch["operation"], "basis": "filesystem-diff",
                                              "request_id": str(req.id)}))
                service_events = 0
                if not result.runtime_killed and sess.status != S.failed.value:
                    try:
                        service_events = self._ingest_service_events(db, sess, live)
                    except Exception:  # noqa: BLE001  (missing events must never break a command)
                        service_events = 0
                db.add(SessionEvent(
                    session_id=session_id,
                    event_type="command_executed",
                    meta={
                        "request_id": str(req.id), "operation": operation, "exit_code": result.exit_code,
                        "timed_out": result.timed_out, "truncated": result.truncated,
                        "runtime_killed": result.runtime_killed, "fs_changes": len(changes),
                        "cwd": live.shell.cwd if stateful else None, "service_events": service_events,
                    },
                ))
                if sess.status != S.failed.value:
                    sess.last_activity_at = _now()
                db.commit()
                return ExecOutcome(
                    req.id, result.exit_code, result.stdout, result.stderr, result.timed_out,
                    result.truncated, result.duration_ms, result.runtime_killed, changes,
                    cwd=live.shell.cwd, service_events=service_events,
                )

    def _exhausted(self, result, processes: list[dict]) -> bool:
        """True when the twin has run out of process slots: the wrapper cannot fork, or the table is full."""
        fork_failed = result.exit_code == 125 and "fork" in result.stderr.lower()
        return fork_failed or len(processes) >= self.policy.pids - 1

    def _fail(self, db: Session, sess: HoneypotSession, live: _Live, reason: str) -> None:
        """Limit violation or runtime loss: terminate the twin and mark the session failed."""
        with self._guard:
            self._live.pop(sess.id, None)
        try:
            self.driver.destroy(live.runtime_id)
        except Exception:  # noqa: BLE001
            pass
        if sess.status != S.failed.value:
            self._transition(db, sess, S.failed, reason)
            sess.ended_at = _now()

    # ---- maintenance -----------------------------------------------------------------------
    def reap(self, now: datetime | None = None) -> list[uuid.UUID]:
        """Destroy sessions past their TTL or idle too long."""
        now = now or _now()
        reaped = []
        with self.db_factory() as db:
            rows = db.scalars(
                select(HoneypotSession).where(HoneypotSession.status.in_([s.value for s in lifecycle.LIVE]))
            ).all()
            victims = []
            for s in rows:
                started = s.started_at if s.started_at.tzinfo else s.started_at.replace(tzinfo=UTC)
                last = s.last_activity_at or started
                last = last if last.tzinfo else last.replace(tzinfo=UTC)
                if now - started > timedelta(seconds=self.policy.session_ttl_s):
                    victims.append((s.id, "ttl expired"))
                elif now - last > timedelta(seconds=self.policy.idle_ttl_s):
                    victims.append((s.id, "idle timeout"))
        for sid, reason in victims:
            self.destroy_session(sid, reason)
            reaped.append(sid)
        return reaped

    def reconcile(self) -> int:
        """After an orchestrator crash/restart: remove orphan runtimes and fail stranded sessions."""
        removed = 0
        for rid in self.driver.list_managed():
            self.driver.destroy(rid)
            removed += 1
        with self.db_factory() as db:
            stranded = db.scalars(
                select(HoneypotSession).where(HoneypotSession.status.in_([s.value for s in lifecycle.LIVE]))
            ).all()
            for s in stranded:
                self._transition(db, s, S.failed, "orchestrator restart")
                s.ended_at = _now()
            db.commit()
        return removed

    def shutdown(self) -> None:
        with self._guard:
            ids = [sid for sid, live in self._live.items() if live is not None]
        for sid in ids:
            try:
                self.destroy_session(sid, "shutdown")
            except Exception:  # noqa: BLE001
                pass
