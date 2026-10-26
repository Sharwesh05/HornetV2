"""Phase H2: migrations up and down on PostgreSQL 16, and the database-level guards (migration 0005).

Starts a throwaway postgres:16-alpine container on a random loopback port. Skipped without Docker.
"""
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app import retention, services
from app.models import HoneypotSession, SessionEvent

BACKEND = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.docker


def _docker():
    try:
        import docker

        c = docker.from_env()
        c.ping()
        return c
    except Exception:
        return None


@pytest.fixture(scope="module")
def pg_url():
    client = _docker()
    if client is None:
        pytest.skip("Docker is not available")
    try:
        client.images.get("postgres:16-alpine")
    except Exception:
        client.images.pull("postgres", tag="16-alpine")
    password = uuid.uuid4().hex
    container = client.containers.run(
        "postgres:16-alpine", detach=True, remove=True, environment={"POSTGRES_PASSWORD": password,
                                                                      "POSTGRES_DB": "hornet_test"},
        ports={"5432/tcp": ("127.0.0.1", None)}, labels={"com.hornet.test": "postgres"})
    try:
        url = None
        for _ in range(120):
            container.reload()
            binding = (container.attrs["NetworkSettings"]["Ports"] or {}).get("5432/tcp")
            if binding:
                url = f"postgresql+psycopg://postgres:{password}@127.0.0.1:{binding[0]['HostPort']}/hornet_test"
                try:
                    with create_engine(url).connect() as conn:
                        conn.execute(text("select 1"))
                    break
                except Exception:
                    pass
            time.sleep(0.5)
        else:
            pytest.fail("PostgreSQL did not start")
        yield url
    finally:
        container.stop(timeout=5)


def alembic(url: str, *args: str) -> str:
    env = {**os.environ, "HORNET_DATABASE_URL": url}
    out = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout + out.stderr


def tables(url: str) -> set[str]:
    with create_engine(url).connect() as conn:
        return set(conn.scalars(text("select tablename from pg_tables where schemaname = 'public'")))


def triggers(url: str) -> set[str]:
    with create_engine(url).connect() as conn:
        return set(conn.scalars(text("select tgrelid::regclass || '.' || tgname from pg_trigger where not tgisinternal")))


def test_migrations_go_up_down_and_up_again(pg_url):
    alembic(pg_url, "upgrade", "head")
    assert {"environment_snapshot", "execution_request", "ai_synthesis", "gateway_event"} <= tables(pg_url)
    assert "execution_observation.hornet_append_only" in triggers(pg_url)
    alembic(pg_url, "downgrade", "base")
    assert tables(pg_url) <= {"alembic_version"} and triggers(pg_url) == set()
    alembic(pg_url, "upgrade", "head")
    assert "0005" in alembic(pg_url, "current")
    assert len(triggers(pg_url)) == 9  # 5 append-only tables, the snapshot table, 3 snapshot-owned tables


@pytest.fixture(scope="module")
def db_factory(pg_url):
    alembic(pg_url, "upgrade", "head")
    return sessionmaker(bind=create_engine(pg_url), expire_on_commit=False)


def refused(factory, sql: str, **params) -> bool:
    with factory() as db:
        try:
            db.execute(text(sql), params)
            db.commit()
            return False
        except DBAPIError as exc:
            db.rollback()
            return "hornet:" in str(exc.orig)


@pytest.fixture
def published(db_factory):
    with db_factory() as db:
        snap = services.create_draft_snapshot(db, None)
        services.validate_snapshot(db, snap)
        services.publish_snapshot(db, snap)
        db.commit()
        return snap


def test_published_snapshots_are_immutable_in_the_database(db_factory, published):
    sid = published.id
    assert refused(db_factory, "update environment_snapshot set profile_hash = 'x' where id = :i", i=sid)
    assert refused(db_factory, "delete from environment_snapshot where id = :i", i=sid)
    assert refused(db_factory, "update virtual_node set permissions = '0777' where snapshot_id = :i", i=sid)
    assert refused(db_factory, "delete from virtual_node where snapshot_id = :i", i=sid)
    assert refused(db_factory, "insert into virtual_node (id, snapshot_id, virtual_path, node_type, sensitivity_class, "
                               "metadata) values (:n, :i, '/planted', 'file', 'public', '{}')", n=uuid.uuid4(), i=sid)
    assert refused(db_factory, "insert into twin_image (id, snapshot_id, image_digest, runtime_type) "
                               "values (:n, :i, 'sha256:x', 'docker')", n=uuid.uuid4(), i=sid)


def test_publishing_a_newer_version_may_archive_the_old_one(db_factory, published):
    with db_factory() as db:
        newer = services.create_draft_snapshot(db, None)
        services.validate_snapshot(db, newer)
        services.publish_snapshot(db, newer)  # archives every other published snapshot: allowed
        db.commit()
    with db_factory() as db:
        assert db.scalar(text("select status from environment_snapshot where id = :i"), {"i": published.id}) == "archived"
    # but archiving is the only change: an archived snapshot is frozen too
    assert refused(db_factory, "update environment_snapshot set status = 'published' where id = :i", i=published.id)


def _session_with_event(factory, snapshot_id: uuid.UUID, days_ago: int = 0) -> uuid.UUID:
    with factory() as db:
        ended = datetime.now(UTC) - timedelta(days=days_ago)
        # PostgreSQL enforces the foreign key (SQLite tests do not), so the visit needs a real snapshot
        s = HoneypotSession(snapshot_id=snapshot_id, status="destroyed", started_at=ended - timedelta(minutes=1),
                            ended_at=ended)
        db.add(s)
        db.flush()
        db.add(SessionEvent(session_id=s.id, event_type="command_executed", meta={}))
        db.commit()
        return s.id


def test_evidence_and_audit_are_append_only_in_the_database(db_factory, published):
    sid = _session_with_event(db_factory, published.id)
    assert refused(db_factory, "update session_event set event_type = 'x' where session_id = :i", i=sid)
    assert refused(db_factory, "delete from session_event where session_id = :i", i=sid)
    with db_factory() as db:
        services.audit(db, "tester", "test.audit", None)
        db.commit()
    assert refused(db_factory, "update admin_audit_event set actor = 'someone-else'")
    assert refused(db_factory, "delete from admin_audit_event")


def test_only_the_retention_purge_may_delete_and_only_in_its_own_transaction(db_factory, published):
    old = _session_with_event(db_factory, published.id, days_ago=40)
    # the flag does not exist outside the purge
    assert refused(db_factory, "delete from session_event where session_id = :i", i=old)
    # even with the flag, append-only rows can never be UPDATED, and the audit log is never deleted
    with db_factory() as db:
        db.execute(text("SET LOCAL hornet.retention_purge = 'on'"))
        with pytest.raises(DBAPIError):
            db.execute(text("update session_event set event_type = 'x' where session_id = :i"), {"i": old})
        db.rollback()
    with db_factory() as db:
        db.execute(text("SET LOCAL hornet.retention_purge = 'on'"))
        with pytest.raises(DBAPIError):
            db.execute(text("delete from admin_audit_event"))
        db.rollback()
    with db_factory() as db:
        counts = retention.purge(db, 30, "tester")  # the real purge
    assert counts["honeypot_session"] >= 1 and counts["session_event"] >= 1
    # and the flag ended with the purge's transaction
    fresh = _session_with_event(db_factory, published.id)
    assert refused(db_factory, "delete from session_event where session_id = :i", i=fresh)
