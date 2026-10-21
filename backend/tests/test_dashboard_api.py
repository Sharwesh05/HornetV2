"""Backend for the Phase 6 dashboard: session tree state, the authenticated live feed, and CORS."""
import json
import time
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.api.live import get_session_factory
from app.config import Settings
from app.models import AdminAuditEvent, GatewayEvent, SessionEvent
from tests.conftest import API_KEY

TREE = [{"path": "/", "node_type": "directory"},
        {"path": "/etc", "parent_path": "/", "node_type": "directory"},
        {"path": "/etc/passwd", "parent_path": "/etc", "node_type": "file"},
        {"path": "/home", "parent_path": "/", "node_type": "directory"},
        {"path": "/home/user", "parent_path": "/home", "node_type": "directory"},
        {"path": "/home/user/notes.txt", "parent_path": "/home/user", "node_type": "file"}]


@pytest.fixture
def twin(client, session_factory):
    from app.api.v1 import get_manager
    from app.main import app
    from app.orchestration.session_manager import SessionManager
    from app.runtime.policy import ExecutionPolicy
    from tests.fakes import FakeDriver

    mgr = SessionManager(FakeDriver(), session_factory, ExecutionPolicy(), "hornet-twin-base:2")
    app.dependency_overrides[get_manager] = lambda: mgr
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    snap = client.post("/api/v1/environments/build", json={"source": "seed", "nodes": TREE}).json()
    client.post(f"/api/v1/environments/{snap['id']}/validate")
    assert client.post(f"/api/v1/environments/{snap['id']}/publish").json()["status"] == "published"
    yield client
    app.dependency_overrides.pop(get_manager, None)
    app.dependency_overrides.pop(get_session_factory, None)


# ---- 6.1 tree state --------------------------------------------------------------------------------------------
def test_tree_state_marks_visited_and_overlay_nodes(twin):
    sid = twin.post("/api/v1/sessions").json()["id"]
    for cmd in ("cat /etc/passwd", "less /etc/passwd", "touch /home/user/notes.txt", "touch /tmp/<img src=x>.sh"):
        twin.post(f"/api/v1/sessions/{sid}/exec", json={"command": cmd})
    r = twin.get(f"/api/v1/sessions/{sid}/tree-state")
    assert r.status_code == 200
    body = r.json()
    nodes = {n["path"]: n for n in body["nodes"]}
    assert body["version"] == 1 and nodes["/"]["visits"] == 0 and nodes["/"]["overlay"] is None
    assert nodes["/etc/passwd"]["visits"] == 2 and nodes["/etc/passwd"]["basis"] == ["command-argument"]
    assert nodes["/etc/passwd"]["in_base"] is True and nodes["/etc/passwd"]["overlay"] is None
    assert nodes["/home/user/notes.txt"]["overlay"] == "created"
    assert {"command-argument", "filesystem-diff"} <= set(nodes["/home/user/notes.txt"]["basis"])
    new = nodes["/tmp/<img src=x>.sh"]  # attacker-named file: not in the base, returned as plain data
    assert new["in_base"] is False and new["id"] is None and new["overlay"] == "created"
    assert new["sensitivity"] == "attacker-created" and nodes["/etc/passwd"]["sensitivity"] == "public"


def test_session_list_and_detail_carry_command_and_gap_counts(twin, session_factory):
    from app.models import AiSynthesis, ExecutionRequest

    sid = twin.post("/api/v1/sessions").json()["id"]
    for cmd in ("id", "ls", "cat /etc/passwd"):
        twin.post(f"/api/v1/sessions/{sid}/exec", json={"command": cmd})
    empty = twin.post("/api/v1/sessions")
    with session_factory() as db:  # one AI proposal recorded for the second command
        req = db.scalars(select(ExecutionRequest).order_by(ExecutionRequest.created_at)).all()[1]
        db.add(AiSynthesis(request_id=req.id, response_mode="proposal", validation_status="accepted", response_metadata={}))
        db.commit()
    listed = {s["id"]: s for s in twin.get("/api/v1/sessions").json()["items"]}
    assert (listed[sid]["commands"], listed[sid]["ai_gaps"]) == (3, 1)
    detail = twin.get(f"/api/v1/sessions/{sid}").json()
    assert (detail["commands"], detail["ai_gaps"]) == (3, 1)
    if empty.status_code == 201:
        assert (listed[empty.json()["id"]]["commands"], listed[empty.json()["id"]]["ai_gaps"]) == (0, 0)


def test_tree_state_404_and_auth(twin):
    assert twin.get(f"/api/v1/sessions/{uuid.uuid4()}/tree-state").status_code == 404
    twin.headers.pop("X-API-Key")
    assert twin.get(f"/api/v1/sessions/{uuid.uuid4()}/tree-state").status_code == 401


# ---- 6.2 live feed ------------------------------------------------------------------------------------------------
def _connect(client, key=API_KEY, origin="http://127.0.0.1:5173"):
    ws = client.websocket_connect("/api/v1/ws/events", headers={"origin": origin} if origin else {})
    conn = ws.__enter__()
    conn.send_text(json.dumps({"api_key": key}))
    return ws, conn


@pytest.mark.parametrize("first_message", [json.dumps({"api_key": "wrong-key-0123456789"}), json.dumps({}),
                                           "not json", json.dumps({"api_key": 123})])
def test_live_feed_refuses_without_the_key(twin, first_message):
    with twin.websocket_connect("/api/v1/ws/events") as ws:
        ws.send_text(first_message)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401


def test_live_feed_refuses_other_origins(twin):
    with pytest.raises(WebSocketDisconnect) as exc, twin.websocket_connect(
            "/api/v1/ws/events", headers={"origin": "https://evil.example"}) as ws:
        ws.receive_json()
    assert exc.value.code == 4403


def test_live_feed_delivers_new_events_within_two_seconds(twin, session_factory):
    sid = uuid.UUID(twin.post("/api/v1/sessions").json()["id"])
    ws, conn = _connect(twin)
    try:
        assert conn.receive_json()["type"] == "ready"
        with session_factory() as db:  # an event written now, by anything (gateway, session manager, another process)
            db.add(SessionEvent(session_id=sid, event_type="command_executed",
                                meta={"note": "<script>alert(1)</script>"}, occurred_at=datetime.now(UTC)))
            db.add(GatewayEvent(connection_id=uuid.uuid4(), source_ip="203.0.113.7", event_type="auth.password",
                                meta={"user": "root"}, occurred_at=datetime.now(UTC)))
            db.commit()
        written = time.monotonic()
        got = {}
        while len(got) < 2:
            msg = conn.receive_json()
            if msg.get("event_type") in ("command_executed", "auth.password") and msg["type"] not in got:
                got[msg["type"]] = (msg, time.monotonic() - written)
        assert all(delay < 2.0 for _, delay in got.values()), got
        event = got["session_event"][0]
        assert event["session_id"] == str(sid) and event["metadata"]["note"] == "<script>alert(1)</script>"  # as data
        assert got["gateway_event"][0]["source_ip"] == "203.0.113.7"
    finally:
        ws.__exit__(None, None, None)
    with session_factory() as db:
        assert db.scalars(select(AdminAuditEvent).where(AdminAuditEvent.action == "live.connect")).first() is not None


def test_live_feed_sends_each_event_once(twin, session_factory):
    """Several polls over the same window must not repeat an event, and a new event still arrives."""
    import threading

    sid = uuid.UUID(twin.post("/api/v1/sessions").json()["id"])
    ws, conn = _connect(twin)
    received: list[dict] = []

    def reader():  # the only reader of this socket; ends when the socket closes
        try:
            while True:
                received.append(conn.receive_json())
        except Exception:  # noqa: BLE001
            return

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        time.sleep(2.5)  # at least two polls over the backlog
        with session_factory() as db:
            db.add(SessionEvent(session_id=sid, event_type="fs_change", meta={"marker": "once"},
                                occurred_at=datetime.now(UTC)))
            db.commit()
        time.sleep(2.5)
    finally:
        ws.__exit__(None, None, None)
        t.join(5)
    ids = [m["id"] for m in received if m["type"] != "ready"]
    assert len(ids) == len(set(ids)), "an event was sent twice"
    assert sum(m.get("metadata", {}).get("marker") == "once" for m in received) == 1
    assert any(m.get("event_type") == "session.ready" for m in received)  # the backlog from session creation


# ---- CORS ------------------------------------------------------------------------------------------------------------
def test_cors_allows_only_the_dashboard_origin(client):
    ok = client.options("/api/v1/sessions", headers={"Origin": "http://127.0.0.1:5173",
                                                     "Access-Control-Request-Method": "GET",
                                                     "Access-Control-Request-Headers": "x-api-key"})
    assert ok.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"
    assert "access-control-allow-credentials" not in ok.headers
    bad = client.options("/api/v1/sessions", headers={"Origin": "https://evil.example",
                                                      "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in bad.headers


def test_wildcard_origins_fail_startup():
    with pytest.raises(ValidationError):
        Settings(admin_api_key="x" * 20, dashboard_origins="*", _env_file=None)
    with pytest.raises(ValidationError):
        Settings(admin_api_key="x" * 20, dashboard_origins="evil.example", _env_file=None)
