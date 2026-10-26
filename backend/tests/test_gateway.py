"""The SSH gateway, driven by a real SSH client against the real server, with the in-memory runtime."""
import asyncio

import asyncssh
import pytest
from sqlalchemy import select

from app import services
from app.config import Settings
from app.gateway.auth import AuthPolicy, ensure_host_key
from app.gateway.server import start_gateway
from app.models import ExecutionRequest, GatewayEvent, HoneypotSession, SessionEvent
from app.orchestration import session_manager as sm
from app.runtime.policy import ExecutionPolicy
from tests.fakes import FakeDriver


def make_env(tmp_path, session_factory, db, **kw):
    snap = services.create_draft_snapshot(db, None)
    services.validate_snapshot(db, snap)
    services.publish_snapshot(db, snap)
    db.commit()
    policy = ExecutionPolicy(**{"max_active_sessions": 5, "idle_ttl_s": 300, **kw.pop("policy", {})})
    driver = FakeDriver()
    mgr = sm.SessionManager(driver, session_factory, policy, "hornet-twin-base:2")
    cfg = dict(admin_api_key="x" * 20, gateway_host_key_path=str(tmp_path / "host_key"), gateway_accept_after=1,
               gateway_weak_passwords="", _env_file=None)
    cfg.update(kw)
    return mgr, driver, Settings(**cfg)


async def connect(gw, username="root", password="pw", **kw):
    return await asyncssh.connect("127.0.0.1", gw.port, username=username, password=password, known_hosts=None,
                                  client_keys=None, agent_path=None, **kw)


def run(coro):
    return asyncio.run(coro)


def events(session_factory, model=GatewayEvent):
    with session_factory() as s:
        q = select(model)
        if hasattr(model, "occurred_at"):
            q = q.order_by(model.occurred_at)
        return list(s.scalars(q))


# ---- login policy ---------------------------------------------------------------------------------------
def test_auth_policy():
    p = AuthPolicy(accept_after=3, weak={"admin"})
    assert [p.accepts(n, "x") for n in (1, 2, 3, 4)] == [False, False, True, True]
    assert p.accepts(1, "admin") and not p.accepts(1, "Admin")
    assert p.describe("hunter2") == {"password": "hunter2"}
    private = AuthPolicy(log_passwords=False).describe("hunter2")
    assert "hunter2" not in str(private) and private["password_len"] == 7 and len(private["password_sha256"]) == 16
    assert len(AuthPolicy().describe("x" * 500)["password"]) == 128


def test_host_key_is_stable_across_restarts(tmp_path):
    path = str(tmp_path / "keys" / "host")
    first = ensure_host_key(path)
    fp1 = asyncssh.read_private_key(first).get_fingerprint()
    assert asyncssh.read_private_key(ensure_host_key(path)).get_fingerprint() == fp1


def test_login_requires_the_accepting_attempt_and_logs_every_try(tmp_path, session_factory, db):
    # password text is opt-in (HORNET_GATEWAY_LOG_PASSWORDS=true); the default keeps only length + hash
    mgr, _, cfg = make_env(tmp_path, session_factory, db, gateway_accept_after=3, gateway_weak_passwords="toor",
                           gateway_log_passwords=True)

    class Tries(asyncssh.SSHClient):
        def __init__(self):
            self.pws = iter(["letmein", "qwerty", "hunter2"])

        def password_auth_requested(self):
            return next(self.pws, None)

    async def main():
        gw = await start_gateway(mgr, cfg, session_factory, port=0)
        try:
            conn, _ = await asyncssh.create_connection(Tries, "127.0.0.1", gw.port, username="admin", known_hosts=None,
                                                       client_keys=None, agent_path=None, public_key_auth=False)
            r = await conn.run("echo hi", check=False)
            conn.close()
            weak = await connect(gw, "root", "toor")      # a known-weak password gets in on the first try
            weak.close()
            with pytest.raises(asyncssh.PermissionDenied):
                await connect(gw, "root", "not-the-weak-one")
            return r
        finally:
            await gw.close()

    assert run(main()).exit_status == 0
    attempts = [(e.meta["attempt"], e.meta["success"], e.meta["password"]) for e in events(session_factory)
                if e.event_type == "auth.password" and e.meta["user"] == "admin"]
    assert attempts == [(1, False, "letmein"), (2, False, "qwerty"), (3, True, "hunter2")]


def test_public_key_logins_are_refused_but_logged(tmp_path, session_factory, db):
    mgr, _, cfg = make_env(tmp_path, session_factory, db)

    async def main():
        gw = await start_gateway(mgr, cfg, session_factory, port=0)
        try:
            key = asyncssh.generate_private_key("ssh-ed25519")
            with pytest.raises(asyncssh.PermissionDenied):
                await asyncssh.connect("127.0.0.1", gw.port, username="root", client_keys=[key], known_hosts=None,
                                       password=None, agent_path=None)
        finally:
            await gw.close()

    run(main())
    ev = [e for e in events(session_factory) if e.event_type == "auth.publickey"]
    assert ev and ev[0].meta["fingerprint"].startswith("SHA256:") and ev[0].meta["success"] is False
    assert not [s for s in events(session_factory, HoneypotSession)]  # a failed login never creates a twin


def test_passwords_can_be_logged_as_hash_only(tmp_path, session_factory, db):
    mgr, _, cfg = make_env(tmp_path, session_factory, db, gateway_log_passwords=False)

    async def main():
        gw = await start_gateway(mgr, cfg, session_factory, port=0)
        try:
            (await connect(gw, "root", "s3cret-pass")).close()
        finally:
            await gw.close()

    run(main())
    blob = str([e.meta for e in events(session_factory)])
    assert "s3cret-pass" not in blob and "password_sha256" in blob


def test_server_identifies_itself_like_openssh(tmp_path, session_factory, db):
    mgr, _, cfg = make_env(tmp_path, session_factory, db)

    async def main():
        gw = await start_gateway(mgr, cfg, session_factory, port=0)
        try:
            r, w = await asyncio.open_connection("127.0.0.1", gw.port)
            banner = (await r.readline()).decode().strip()
            w.close()
            return banner
        finally:
            await gw.close()

    assert run(main()) == "SSH-2.0-OpenSSH_9.2p1 Debian-2+deb12u3"


# ---- non-interactive: ssh host "command" ------------------------------------------------------------------------
def test_exec_returns_output_and_records_evidence(tmp_path, session_factory, db):
    mgr, driver, cfg = make_env(tmp_path, session_factory, db)

    async def main():
        gw = await start_gateway(mgr, cfg, session_factory, port=0)
        try:
            conn = await connect(gw)
            ok = await conn.run("echo hello", check=False)
            bad = await conn.run("nonsense-command", check=False)
            conn.close()
            await conn.wait_closed()
            return ok, bad
        finally:
            await gw.close()

    ok, bad = run(main())
    assert ok.stdout == "hello\n" and ok.exit_status == 0
    assert bad.exit_status == 0  # the fake wrapper returns 0 for unknown lines; real exit codes are covered with Docker
    with session_factory() as s:
        reqs = s.scalars(select(ExecutionRequest)).all()
        assert [r.operation for r in reqs] == ["exec_command", "exec_command"]
        sess = s.scalars(select(HoneypotSession)).one()
        assert sess.status == "destroyed" and sess.end_reason == "disconnect" and sess.source_ip == "127.0.0.1"
    assert driver.destroyed and driver.runtimes == {}      # the twin is destroyed when the connection closes
    kinds = [e.event_type for e in events(session_factory)]
    assert kinds[0] == "connection.opened" and "session.created" in kinds and kinds[-1] == "connection.closed"


def test_each_connection_gets_its_own_clean_twin(tmp_path, session_factory, db):
    mgr, driver, cfg = make_env(tmp_path, session_factory, db)

    async def main():
        gw = await start_gateway(mgr, cfg, session_factory, port=0)
        try:
            a, b = await connect(gw), await connect(gw)
            await a.run("touch /tmp/only-in-a", check=False)
            await b.run("echo x", check=False)
            both = {rt["spec"].session_id: sorted(rt["files"]) for rt in driver.runtimes.values()}
            a.close()
            await a.wait_closed()
            c = await connect(gw)                       # reconnecting after the first one dropped: a brand new twin
            await c.run("echo y", check=False)
            fresh = [sorted(rt["files"]) for rt in driver.runtimes.values() if "/tmp/only-in-a" in rt["files"]]
            c.close()
            b.close()
            return both, fresh
        finally:
            await gw.close()

    both, fresh = run(main())
    assert len(both) == 2 and sorted(map(len, both.values())) == [0, 1]
    assert any("/tmp/only-in-a" in files for files in both.values()) and sum("/tmp/only-in-a" in f for f in both.values()) == 1
    assert fresh == []                                  # the new connection's twin does not have the old file


def test_ssh_multiplexed_channels_share_one_twin(tmp_path, session_factory, db):
    mgr, driver, cfg = make_env(tmp_path, session_factory, db)

    async def main():
        gw = await start_gateway(mgr, cfg, session_factory, port=0)
        try:
            conn = await connect(gw)
            await asyncio.gather(*(conn.run(f"echo {i}", check=False) for i in range(5)))
            n = len(driver.runtimes)
            conn.close()
            return n
        finally:
            await gw.close()

    assert run(main()) == 1


# ---- interactive shell ---------------------------------------------------------------------------------------------
async def read_until(stream, marker, timeout=5):
    buf = ""
    async def go():
        nonlocal buf
        while marker not in buf:
            chunk = await stream.read(1024)
            if not chunk:
                break
            buf += chunk
    await asyncio.wait_for(go(), timeout)
    return buf


def test_interactive_shell_keeps_directory_and_variables(tmp_path, session_factory, db):
    mgr, driver, cfg = make_env(tmp_path, session_factory, db)

    async def main():
        gw = await start_gateway(mgr, cfg, session_factory, port=0)
        try:
            conn = await connect(gw)
            proc = await conn.create_process(term_type="xterm", term_size=(100, 30))
            screen = await read_until(proc.stdout, "$ ")
            proc.stdin.write("cd /var/log\n")
            screen += await read_until(proc.stdout, "/var/log$ ")
            proc.stdin.write("export WHO=attacker\n")
            await read_until(proc.stdout, "$ ")
            proc.stdin.write("pwd\n")
            screen += await read_until(proc.stdout, "$ ")
            proc.stdin.write("echo $WHO\n")
            screen += await read_until(proc.stdout, "attacker")
            proc.stdin.write("exit\n")
            screen += await read_until(proc.stdout, "logout")
            await proc.wait()
            conn.close()
            return screen, proc.exit_status
        finally:
            await gw.close()

    screen, status = run(main())
    assert status == 0 and "Last login:" in screen
    assert "user@srv-" in screen and ":~$ " in screen and "/var/log$ " in screen   # prompt follows the directory
    assert screen.count("/var/log\r\n") >= 1 and "attacker" in screen
    with session_factory() as s:
        ops = {r.operation for r in s.scalars(select(ExecutionRequest))}
        assert ops == {"login_banner", "interactive_input"}
        cwds = [e.meta.get("cwd") for e in s.scalars(select(SessionEvent)) if e.event_type == "command_executed"]
        assert "/var/log" in cwds


def test_piped_shell_without_a_terminal(tmp_path, session_factory, db):
    mgr, _, cfg = make_env(tmp_path, session_factory, db)

    async def main():
        gw = await start_gateway(mgr, cfg, session_factory, port=0)
        try:
            conn = await connect(gw)
            r = await conn.run(input="cd /tmp\npwd\necho done\nexit\n", check=False)   # `ssh host` with commands on stdin
            conn.close()
            return r
        finally:
            await gw.close()

    r = run(main())
    assert "/tmp" in r.stdout and "done" in r.stdout and "$ " not in r.stdout       # no prompt without a terminal


def test_idle_shell_logs_out(tmp_path, session_factory, db):
    mgr, driver, cfg = make_env(tmp_path, session_factory, db, policy={"idle_ttl_s": 5})
    cfg_idle = cfg

    async def main():
        gw = await start_gateway(mgr, cfg_idle, session_factory, port=0)
        mgr.policy = ExecutionPolicy(idle_ttl_s=5)
        try:
            conn = await connect(gw)
            proc = await conn.create_process(term_type="xterm")
            await read_until(proc.stdout, "$ ")
            msg = await read_until(proc.stdout, "auto-logout", timeout=12)
            await proc.wait()
            conn.close()
            return msg
        finally:
            await gw.close()

    assert "timed out waiting for input" in run(main())


def test_overlong_command_is_rejected_politely(tmp_path, session_factory, db):
    mgr, _, cfg = make_env(tmp_path, session_factory, db)

    async def main():
        gw = await start_gateway(mgr, cfg, session_factory, port=0)
        try:
            conn = await connect(gw)
            r = await conn.run("echo " + "A" * 20_000, check=False)
            ok = await conn.run("echo still-here", check=False)
            conn.close()
            return r, ok
        finally:
            await gw.close()

    r, ok = run(main())
    assert "command too long" in r.stderr and r.exit_status == 1 and ok.stdout == "still-here\n"


def test_limit_violation_ends_the_connection(tmp_path, session_factory, db):
    mgr, driver, cfg = make_env(tmp_path, session_factory, db)

    async def main():
        gw = await start_gateway(mgr, cfg, session_factory, port=0)
        try:
            conn = await connect(gw)
            await conn.run("saturate", check=False)             # exhausts the twin's process table
            await asyncio.wait_for(conn.wait_closed(), 5)       # the gateway hangs up
        finally:
            await gw.close()

    run(main())
    with session_factory() as s:
        sess = s.scalars(select(HoneypotSession)).one()
        assert sess.status == "destroyed" and driver.runtimes == {}


# ---- refusals and limits ---------------------------------------------------------------------------------------------
def test_sftp_scp_and_forwarding_are_refused_and_logged(tmp_path, session_factory, db):
    mgr, driver, cfg = make_env(tmp_path, session_factory, db)
    local_file = tmp_path / "upload.txt"
    local_file.write_text("payload")

    async def main():
        gw = await start_gateway(mgr, cfg, session_factory, port=0)
        try:
            conn = await connect(gw)
            with pytest.raises(asyncssh.Error):
                async with conn.start_sftp_client():
                    pass
            with pytest.raises(asyncssh.Error):
                await asyncssh.scp(str(local_file), (conn, "/tmp/x"))
            with pytest.raises(asyncssh.Error):
                await conn.open_connection("10.0.0.5", 3306)          # a request to tunnel into the internal network
            with pytest.raises(asyncssh.ChannelListenError):
                await conn.forward_remote_port("", 0, "127.0.0.1", 22)
            conn.close()
        finally:
            await gw.close()

    run(main())
    kinds = [e.event_type for e in events(session_factory)]
    assert "forward.denied" in kinds or "subsystem.denied" in kinds
    assert not any(rt["files"] for rt in driver.runtimes.values())     # nothing was written anywhere


def test_per_address_connection_limit(tmp_path, session_factory, db):
    mgr, _, cfg = make_env(tmp_path, session_factory, db, gateway_max_connections_per_ip=2)

    async def main():
        gw = await start_gateway(mgr, cfg, session_factory, port=0)
        try:
            a, b = await connect(gw), await connect(gw)
            # the server drops the over-limit connection: asyncssh reports it as asyncssh.Error on Windows,
            # but on Linux the reset arrives first and surfaces as ConnectionResetError (an OSError)
            with pytest.raises((asyncssh.Error, OSError)):
                await connect(gw)
            a.close()
            await a.wait_closed()
            await asyncio.sleep(0.2)
            c = await connect(gw)                               # a slot freed up
            c.close()
            b.close()
        finally:
            await gw.close()

    run(main())
    assert any(e.event_type == "limit.rejected" for e in events(session_factory))


def test_twin_capacity_is_enforced_without_breaking_the_gateway(tmp_path, session_factory, db):
    mgr, driver, cfg = make_env(tmp_path, session_factory, db, policy={"max_active_sessions": 1})

    async def main():
        gw = await start_gateway(mgr, cfg, session_factory, port=0)
        try:
            a = await connect(gw)
            await a.run("echo a", check=False)
            b = await connect(gw)
            r = await b.run("echo b", check=False)               # no capacity: refused, not crashed
            b.close()
            ok = await a.run("echo still-works", check=False)
            a.close()
            return r, ok
        finally:
            await gw.close()

    r, ok = run(main())
    assert r.exit_status == 255 and r.stdout == "" and ok.stdout == "still-works\n"
    refused = [e for e in events(session_factory) if e.event_type == "session.refused"]
    assert refused and refused[0].meta["reason"] == "SessionLimitReached"


def test_closing_the_gateway_leaves_no_twins_behind(tmp_path, session_factory, db):
    mgr, driver, cfg = make_env(tmp_path, session_factory, db)

    async def main():
        gw = await start_gateway(mgr, cfg, session_factory, port=0)
        conn = await connect(gw)
        await conn.run("echo x", check=False)
        assert len(driver.runtimes) == 1
        conn.close()
        await conn.wait_closed()
        await gw.close()

    run(main())
    assert driver.runtimes == {} and mgr.active_count() == 0


def test_gateway_must_stay_on_loopback_outside_prod():
    with pytest.raises(ValueError):
        Settings(admin_api_key="x" * 20, gateway_enabled=True, gateway_host="0.0.0.0", _env_file=None)  # noqa: S104
    Settings(admin_api_key="x" * 40, gateway_enabled=True, gateway_host="0.0.0.0", env="prod",         # noqa: S104
             database_url="postgresql://x", dashboard_origins="https://hornet.example.org", retention_auto=True,
             _env_file=None)


# ---- integration with the application -----------------------------------------------------------------------------
def test_gateway_starts_with_the_api_and_its_logins_are_visible_to_admins(tmp_path, session_factory, db, monkeypatch):
    import socket

    from fastapi.testclient import TestClient

    import app.db
    import app.main
    import app.runtime.factory as factory

    mgr, driver, cfg = make_env(tmp_path, session_factory, db, gateway_enabled=True, gateway_port=_free_port())
    monkeypatch.setattr(app.main, "get_settings", lambda: cfg)
    monkeypatch.setattr(app.db, "get_sessionmaker", lambda: session_factory)
    monkeypatch.setattr(factory, "build_manager", lambda settings, db_factory: mgr)
    application = app.main.create_app()
    headers = {"X-API-Key": "test-key-0123456789abcdef"}

    with TestClient(application, headers=headers) as client:
        from app.api.v1 import get_db
        application.dependency_overrides[get_db] = lambda: (yield_session(session_factory))
        port = application.state.gateway.port
        assert port == cfg.gateway_port
        assert client.get("/api/v1/health").json()["gateway"] == f"listening:{port}"

        async def attack():
            conn = await asyncssh.connect("127.0.0.1", port, username="oracle", password="Sup3r-Secret", known_hosts=None,
                                          client_keys=None, agent_path=None)
            await conn.run("echo hi", check=False)
            conn.close()
            await conn.wait_closed()
            await asyncio.sleep(0.3)

        asyncio.run(attack())
        page = client.get("/api/v1/gateway/events?event_type=auth.password").json()
        assert page["total"] == 1 and page["items"][0]["metadata"]["user"] == "oracle"
        assert page["items"][0]["source_ip"] == "127.0.0.1"
        assert client.get("/api/v1/gateway/events?source_ip=203.0.113.9").json()["total"] == 0
        assert client.get("/api/v1/gateway/events?limit=0").status_code == 422
        client.headers.pop("X-API-Key")
        assert client.get("/api/v1/gateway/events").status_code == 401                # admin only

    with socket.socket() as s:                                  # the port is released and no twin survives
        assert s.connect_ex(("127.0.0.1", cfg.gateway_port)) != 0
    assert driver.runtimes == {}


def _free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def yield_session(session_factory):
    return session_factory().__enter__()
