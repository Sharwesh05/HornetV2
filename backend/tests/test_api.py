import uuid

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import HoneypotSession, SessionEvent, SessionStatus


def test_health_is_public(client):
    r = client.get("/api/v1/health", headers={"X-API-Key": ""})
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "up", "runtime": "not started", "gateway": "off"}


@pytest.mark.parametrize("key", [None, "", "wrong-key-0123456789"])
def test_admin_routes_require_key(client, key):
    headers = {"X-API-Key": key} if key is not None else {}
    client.headers.pop("X-API-Key", None)
    for path in ("/api/v1/environments", "/api/v1/sessions", "/api/v1/analytics/overview"):
        assert client.get(path, headers=headers).status_code == 401
    assert client.post("/api/v1/environments/build", headers=headers).status_code == 401


def test_build_validate_publish_flow(client):
    r = client.post("/api/v1/environments/build")
    assert r.status_code == 201
    snap = r.json()
    assert snap["status"] == "draft" and snap["version"] == 1

    assert client.post(f"/api/v1/environments/{snap['id']}/publish").status_code == 409

    v = client.post(f"/api/v1/environments/{snap['id']}/validate").json()
    assert v["status"] == "validated" and v["validation_report"]["ok"] is True

    p = client.post(f"/api/v1/environments/{snap['id']}/publish").json()
    assert p["status"] == "published" and p["published_at"]

    tree = client.get(f"/api/v1/environments/{snap['id']}/tree").json()
    paths = {n["path"] for n in tree["nodes"]}
    assert "/" in paths and "/var/www" in paths
    root = next(n for n in tree["nodes"] if n["path"] == "/")
    assert root["parent_id"] is None

    # second build archives the first on publish
    s2 = client.post("/api/v1/environments/build").json()
    client.post(f"/api/v1/environments/{s2['id']}/validate")
    client.post(f"/api/v1/environments/{s2['id']}/publish")
    statuses = {e["version"]: e["status"] for e in client.get("/api/v1/environments").json()}
    assert statuses == {1: "archived", 2: "published"}
    assert client.get("/api/v1/analytics/overview").json()["published_version"] == 2


def test_build_rejects_path_escape(client):
    body = {"nodes": [{"path": "/", "node_type": "directory"},
                      {"path": "/a/../../etc", "parent_path": "/", "node_type": "directory"}]}
    assert client.post("/api/v1/environments/build", json=body).status_code == 422


def test_invalid_tree_stays_draft(client):
    body = {"nodes": [{"path": "/", "node_type": "directory"},
                      {"path": "/x/y", "parent_path": "/x", "node_type": "file"}]}
    # parent "/x" is not defined, so creation maps it to no parent -> two roots
    snap = client.post("/api/v1/environments/build", json=body).json()
    v = client.post(f"/api/v1/environments/{snap['id']}/validate").json()
    assert v["status"] == "draft" and v["validation_report"]["ok"] is False
    assert client.post(f"/api/v1/environments/{snap['id']}/publish").status_code == 409


def test_unknown_ids_404(client):
    missing = uuid.uuid4()
    assert client.get(f"/api/v1/environments/{missing}").status_code == 404
    assert client.get(f"/api/v1/sessions/{missing}").status_code == 404
    assert client.get(f"/api/v1/sessions/{missing}/events").status_code == 404


def test_sessions_events_and_overview(client, session_factory):
    snap = client.post("/api/v1/environments/build").json()
    client.post(f"/api/v1/environments/{snap['id']}/validate")
    client.post(f"/api/v1/environments/{snap['id']}/publish")

    with session_factory() as db:
        sess = HoneypotSession(snapshot_id=uuid.UUID(snap["id"]), status=SessionStatus.active.value)
        db.add(sess)
        db.flush()
        db.add_all([SessionEvent(session_id=sess.id, event_type="command", meta={"i": i}) for i in range(5)])
        db.commit()
        sid = str(sess.id)

    page = client.get(f"/api/v1/sessions/{sid}/events?limit=2&offset=1").json()
    assert page["total"] == 5 and len(page["items"]) == 2 and page["offset"] == 1
    assert client.get("/api/v1/sessions").json()["total"] == 1
    assert client.get("/api/v1/sessions?limit=0").status_code == 422

    ov = client.get("/api/v1/analytics/overview").json()
    assert ov["sessions_active"] == 1 and ov["events_by_type"] == {"command": 5}


def test_admin_actions_are_audited(client):
    snap = client.post("/api/v1/environments/build").json()
    client.post(f"/api/v1/environments/{snap['id']}/validate")
    actions = [a["action"] for a in client.get("/api/v1/audit").json()["items"]]
    assert {"environment.build", "environment.validate"} <= set(actions)


@pytest.mark.parametrize("key", ["", "short", "change-me", "changeme"])
def test_unsafe_api_key_fails_startup(key):
    with pytest.raises(ValidationError):
        Settings(admin_api_key=key)


def test_sqlite_rejected_in_prod():
    with pytest.raises(ValidationError):
        Settings(admin_api_key="x" * 20, env="prod", database_url="sqlite:///a.db")


# ---- twin session endpoints (fake driver; real Docker is covered by test_docker_isolation) ----
@pytest.fixture
def twin_client(client, session_factory):
    from app.api.v1 import get_manager
    from app.main import app
    from app.orchestration.session_manager import SessionManager
    from app.runtime.policy import ExecutionPolicy
    from tests.fakes import FakeDriver

    mgr = SessionManager(FakeDriver(), session_factory, ExecutionPolicy(max_active_sessions=1), "hornet-twin-base:2")
    app.dependency_overrides[get_manager] = lambda: mgr
    yield client
    app.dependency_overrides.pop(get_manager, None)


def _publish(client):
    snap = client.post("/api/v1/environments/build").json()
    client.post(f"/api/v1/environments/{snap['id']}/validate")
    client.post(f"/api/v1/environments/{snap['id']}/publish")
    return snap


def test_session_endpoints_flow(twin_client):
    c = twin_client
    assert c.post("/api/v1/sessions").status_code == 409  # nothing published yet
    _publish(c)
    s = c.post("/api/v1/sessions")
    assert s.status_code == 201 and s.json()["status"] == "ready"
    sid = s.json()["id"]
    assert c.post("/api/v1/sessions").status_code == 429  # limit of 1

    r = c.post(f"/api/v1/sessions/{sid}/exec", json={"command": "echo hi"}).json()
    assert r["stdout"] == "hi\n" and r["source"] == "execution" and r["fs_changes"][0]["operation"] == "created"
    assert c.post(f"/api/v1/sessions/{sid}/exec", json={"command": ""}).status_code == 422

    d = c.delete(f"/api/v1/sessions/{sid}")
    assert d.status_code == 200 and d.json()["status"] == "destroyed"
    assert c.post(f"/api/v1/sessions/{sid}/exec", json={"command": "echo hi"}).status_code == 409
    assert c.delete(f"/api/v1/sessions/{uuid.uuid4()}").status_code == 404
    actions = {a["action"] for a in c.get("/api/v1/audit").json()["items"]}
    assert {"session.create", "session.destroy"} <= actions


def test_session_endpoints_need_auth(twin_client):
    twin_client.headers.pop("X-API-Key", None)
    assert twin_client.post("/api/v1/sessions").status_code == 401
    assert twin_client.post(f"/api/v1/sessions/{uuid.uuid4()}/exec", json={"command": "id"}).status_code == 401
    assert twin_client.delete(f"/api/v1/sessions/{uuid.uuid4()}").status_code == 401


# ---- build from a discovered Docker environment (fakes; real Docker is in test_docker_build) ----
@pytest.fixture
def docker_client(client):
    from app.api.v1 import get_docker_services
    from app.main import app
    from tests.helpers_discovery import FakeImageBuilder, FakeReadOnlyDocker

    holder = {"docker": FakeReadOnlyDocker(), "builder": FakeImageBuilder()}
    app.dependency_overrides[get_docker_services] = lambda: (lambda: (holder["docker"], holder["builder"]))
    client.holder = holder
    yield client
    app.dependency_overrides.pop(get_docker_services, None)


def test_build_from_docker_end_to_end(docker_client):
    from tests.helpers_discovery import CANARIES

    r = docker_client.post("/api/v1/environments/build", json={"source": "docker", "project": "shop-prod"})
    assert r.status_code == 201, r.text
    snap = r.json()
    assert snap["status"] == "validated"
    assert snap["validation_report"]["build"]["image"]["digest"].startswith("sha256:")
    assert not any(c in r.text for c in CANARIES.values())  # nothing planted comes back out

    tree = docker_client.get(f"/api/v1/environments/{snap['id']}/tree").json()["nodes"]
    paths = {n["path"] for n in tree}
    assert {"/services/web", "/databases/shop", "/home/user/.pgpass", "/etc/passwd"} <= paths

    assert docker_client.post(f"/api/v1/environments/{snap['id']}/publish").json()["status"] == "published"
    actions = {a["action"] for a in docker_client.get("/api/v1/audit").json()["items"]}
    assert "environment.build" in actions


def test_build_from_docker_refuses_unlisted_project(docker_client):
    r = docker_client.post("/api/v1/environments/build", json={"source": "docker", "project": "payroll"})
    assert r.status_code == 403
    assert docker_client.get("/api/v1/environments").json() == []
    assert docker_client.holder["builder"].built == []


def test_build_from_docker_requires_project_and_auth(docker_client):
    assert docker_client.post("/api/v1/environments/build", json={"source": "docker"}).status_code == 422
    assert docker_client.post("/api/v1/environments/build", json={"source": "bogus"}).status_code == 422
    docker_client.headers.pop("X-API-Key", None)
    assert docker_client.post("/api/v1/environments/build", json={"source": "docker", "project": "shop-prod"}).status_code == 401
    assert docker_client.post("/api/v1/discovery/preview", json={"project": "shop-prod"}).status_code == 401


def test_failed_docker_build_reports_stage_and_publishes_nothing(docker_client):
    from tests.helpers_discovery import FakeImageBuilder

    docker_client.holder["builder"] = FakeImageBuilder(fail=True)
    r = docker_client.post("/api/v1/environments/build", json={"source": "docker", "project": "shop-prod"})
    assert r.status_code == 422 and r.json()["detail"]["stage"] == "image"
    assert docker_client.get("/api/v1/environments").json() == []
    actions = [a["action"] for a in docker_client.get("/api/v1/audit").json()["items"]]
    assert "environment.build_failed" in actions


def test_discovery_preview_returns_only_sanitized_data(docker_client):
    from tests.helpers_discovery import CANARIES

    r = docker_client.post("/api/v1/discovery/preview", json={"project": "shop-prod"})
    assert r.status_code == 200
    body = r.json()
    assert {s["role"] for s in body["services"]} == {"http-app", "postgres", "redis"}
    assert not any(c in r.text for c in CANARIES.values())
    assert docker_client.holder["builder"].built == []  # preview builds nothing
    assert docker_client.post("/api/v1/discovery/preview", json={"project": "other"}).status_code == 403


def test_seed_builds_do_not_need_docker(client):
    assert client.post("/api/v1/environments/build", json={"source": "seed"}).status_code == 201


def test_test_suite_is_hermetic_and_never_uses_a_real_model():
    """Guards against a developer's .env (HORNET_AI_PROVIDER=nim + a real key) leaking into unit tests."""
    from app.config import get_settings

    settings = get_settings()
    assert settings.ai_provider == "template" and settings.sanitizer_mode == "off"
