"""Phase H: the full escape and abuse suite (H4) and concurrency / restart / reaper behaviour (H1), on real Docker.

The H4 tests run once per available runtime: hardened Docker (runc) always, gVisor (runsc) when Docker has it
(H6). Every probe is something a visitor could type; each assertion is about what must NOT be possible.
Skipped when Docker or hornet-twin-base:2 is missing.
"""
import asyncio
import time
from datetime import UTC, datetime, timedelta

import asyncssh
import pytest
from sqlalchemy import select

from app import services
from app.config import Settings
from app.gateway.server import start_gateway
from app.models import GatewayEvent, HoneypotSession
from app.orchestration import session_manager as sm
from app.runtime.policy import ExecutionPolicy

IMAGE = "hornet-twin-base:2"


def _runtimes() -> set[str]:
    try:
        from app.runtime.docker_driver import HardenedDockerDriver

        d = HardenedDockerDriver()
        d.resolve_image(IMAGE)
        return d.available_runtimes() if d.ping() else set()
    except Exception:
        return set()


RUNTIMES = _runtimes()
pytestmark = [pytest.mark.docker,
              pytest.mark.skipif(not RUNTIMES, reason=f"Docker daemon or image {IMAGE} not available")]
DRIVERS = [pytest.param("hardened-docker", id="runc"),
           pytest.param("gvisor", id="gvisor",
                        marks=pytest.mark.skipif("runsc" not in RUNTIMES, reason="gVisor (runsc) is not installed"))]


def _driver(name: str):
    from app.runtime.docker_driver import GVisorDriver, HardenedDockerDriver

    return GVisorDriver() if name == "gvisor" else HardenedDockerDriver()


@pytest.fixture
def published(db):
    snap = services.create_draft_snapshot(db, None)
    services.validate_snapshot(db, snap)
    services.publish_snapshot(db, snap)
    db.commit()
    return snap


@pytest.fixture
def managers(session_factory):
    made: list[sm.SessionManager] = []

    def make(driver: str = "hardened-docker", **policy) -> sm.SessionManager:
        m = sm.SessionManager(_driver(driver), session_factory, ExecutionPolicy(**{"timeout_ms": 5000, **policy}), IMAGE)
        made.append(m)
        return m

    yield make
    for m in made:
        m.shutdown()
        m.reconcile()


@pytest.fixture(params=DRIVERS)
def twin(request, published, managers):
    mgr = managers(request.param)
    sess = mgr.create_session()

    def run(cmd):
        return mgr.execute(sess.id, cmd)

    run.runtime_id = sess.runtime_id  # type: ignore[attr-defined]
    return request.param, run


def fails(out) -> bool:
    return out.exit_code != 0


# ======================================== H4: escape attempts ==========================================
def test_no_capabilities_and_no_new_privileges(twin):
    name, run = twin
    status = run("grep -E '^(CapInh|CapPrm|CapEff|CapBnd|CapAmb|NoNewPrivs):' /proc/self/status").stdout
    fields = dict(line.split(":\t") for line in status.strip().splitlines())
    # gVisor's /proc/self/status has no CapAmb line; ambient capabilities are a subset of CapPrm, checked anyway
    for cap in ("CapPrm", "CapEff", "CapBnd", *(["CapAmb"] if "CapAmb" in fields else [])):
        assert int(fields[cap].strip(), 16) == 0, f"{cap} = {fields[cap]}"
    assert name == "gvisor" or "CapAmb" in fields
    if "NoNewPrivs" in fields:
        assert fields["NoNewPrivs"].strip() == "1"
    else:  # gVisor does not report it in /proc; check what Docker was told instead
        import docker

        opts = docker.from_env().containers.get(run.runtime_id).attrs["HostConfig"]["SecurityOpt"]
        assert name == "gvisor" and any(o.startswith("no-new-privileges") for o in opts)


def test_seccomp_filter_is_active(twin):
    name, run = twin
    seccomp = run("grep -E '^Seccomp:' /proc/self/status").stdout.split()
    if name == "gvisor":
        # gVisor implements system calls itself; the container's calls never reach the host kernel's seccomp path
        pytest.skip("accepted: not meaningful under gVisor (the Sentry filters host system calls itself)")
    assert seccomp == ["Seccomp:", "2"]  # 2 = filter mode (Docker's default profile)


def test_no_setuid_or_setgid_programs(twin):
    _, run = twin
    out = run("find / -xdev \\( -perm -4000 -o -perm -2000 \\) -type f 2>/dev/null | head -5")
    assert out.stdout.strip() == ""


def test_no_docker_socket_host_paths_or_host_devices(twin):
    _, run = twin
    assert fails(run("test -e /var/run/docker.sock || test -e /run/docker.sock"))
    assert run("grep -cE 'docker.sock|/Users/|/mnt/c' /proc/mounts").stdout.strip() == "0"
    # the visitor's home is a size-limited tmpfs; nothing under /home may come from the host
    assert run("awk '$2 ~ \"^/home\" && $3 != \"tmpfs\"' /proc/mounts").stdout.strip() == ""
    assert fails(run("head -c1 /dev/mem")) and fails(run("head -c1 /dev/kmsg"))
    assert fails(run("ls /dev/sda /dev/nvme0n1 /dev/vda 2>/dev/null"))


@pytest.mark.parametrize("probe", [
    "echo 1 > /proc/sys/kernel/panic",
    "echo c > /proc/sysrq-trigger",
    "echo 1 > /sys/kernel/mm/ksm/run",
    "touch /etc/hornet-probe",
    "touch /usr/bin/hornet-probe",
])
def test_kernel_and_system_files_are_not_writable(twin, probe):
    _, run = twin
    assert fails(run(f"{probe} 2>/dev/null")), probe


@pytest.mark.parametrize("probe", [
    "mount -t tmpfs none /mnt",
    "unshare -Ur true",
    "unshare -m true",
    "mknod /tmp/kmem c 1 2",
    "python3 -c \"import socket; socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)\"",
    "python3 -c \"import socket; socket.socket(socket.AF_PACKET, socket.SOCK_RAW)\"",
    "kill -9 1",
    "chroot / true",
])
def test_privileged_operations_are_refused(twin, probe):
    name, run = twin
    if name == "gvisor" and probe in GVISOR_SANDBOXED:
        # accepted exception: gVisor implements these inside its own kernel (the Sentry), so they succeed there
        # but give nothing on the host; test_gvisor_namespaces_and_devices_stay_inside_the_sandbox checks that
        pytest.skip(f"accepted: under gVisor '{probe}' runs inside the Sentry (see the threat model)")
    assert fails(run(f"{probe} 2>/dev/null")), probe


GVISOR_SANDBOXED = {"unshare -Ur true", "mknod /tmp/kmem c 1 2"}


def test_host_kernel_memory_is_not_readable(twin):
    _, run = twin
    # runc masks /proc/kcore with an empty /dev/null; gVisor has no /proc/kcore at all
    assert run("head -c1 /proc/kcore 2>/dev/null | wc -c").stdout.strip() == "0"


def test_gvisor_namespaces_and_devices_stay_inside_the_sandbox(twin):
    name, run = twin
    if name != "gvisor":
        pytest.skip("accepted: runc refuses unshare and mknod outright (test_privileged_operations_are_refused)")
    # "root" in a new user namespace still cannot mount, write kernel settings or reach host memory
    inside = "unshare -Ur sh -c '{}'"
    for probe in ("mount -t tmpfs none /mnt", "echo 1 > /proc/sys/kernel/panic", "head -c1 /dev/mem"):
        assert fails(run(inside.format(probe) + " 2>/dev/null")), probe
    # a device node can be created on the tmpfs, but it is backed by nothing: opening it fails
    assert fails(run("mknod /tmp/kmem c 1 2 2>/dev/null; head -c1 /tmp/kmem 2>/dev/null"))


def test_cannot_read_the_fake_services_or_pid1_environment(twin):
    _, run = twin
    assert fails(run("cat /proc/1/environ")) and fails(run("ls /proc/1/root/"))


# ======================================== H4: egress (both runtimes) =====================================
@pytest.mark.parametrize("probe", [
    "getent hosts example.com",
    "timeout 3 bash -c 'echo x > /dev/udp/1.1.1.1/53'",
    "ping -c 1 -W 2 1.1.1.1",
    "timeout 3 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443'",
    "timeout 3 bash -c 'exec 3<>/dev/tcp/2606:4700:4700::1111/443'",
    "curl -m 3 -sS http://169.254.169.254/latest/meta-data/",
    "timeout 3 bash -c 'exec 3<>/dev/tcp/172.17.0.1/2375'",
])
def test_no_egress(twin, probe):
    _, run = twin
    assert fails(run(f"{probe} >/dev/null 2>&1")), probe


# ======================================== H4: exhaustion ==================================================
def test_disk_fill_stops_at_the_writable_limit(twin):
    _, run = twin
    out = run("dd if=/dev/zero of=/tmp/fill bs=1M count=64 2>&1; ls -l /tmp/fill | awk '{print $5}'")
    size = int(out.stdout.strip().splitlines()[-1])
    assert size <= 16 * 1024 * 1024 and "No space left" in out.stdout
    assert run("echo still-usable").stdout.strip() == "still-usable"


def test_long_running_loop_is_stopped(twin):
    _, run = twin
    started = time.monotonic()
    out = run("while true; do :; done")
    assert out.timed_out and time.monotonic() - started < 15
    assert run("echo next").stdout.strip() == "next"


def test_output_flood_is_capped(twin):
    _, run = twin
    out = run("yes hornet")
    assert out.truncated and len(out.stdout) <= 65_536


def test_memory_hog_is_contained(twin):
    _, run = twin
    # the bytes must be touched: bytearray(n) is lazily zeroed and never counts against the limit
    out = run("python3 -c \"x = b'x' * (512 * 1024 * 1024); print('allocated')\"")
    assert "allocated" not in out.stdout


def test_fork_bomb_ends_the_twin_not_the_host(published, managers):
    for driver in ["hardened-docker", *(["gvisor"] if "runsc" in RUNTIMES else [])]:
        mgr = managers(driver, timeout_ms=3000)
        s = mgr.create_session()
        mgr.execute(s.id, ":(){ :|:& };:")
        fresh = mgr.create_session()                      # the host and the next visitor are fine
        assert mgr.execute(fresh.id, "echo ok").stdout.strip() == "ok", driver


# ======================================== H1: many visitors, restarts, reaper ===========================
def _cfg(tmp_path, **kw) -> Settings:
    return Settings(admin_api_key="x" * 20, _env_file=None, gateway_accept_after=1, gateway_weak_passwords="",
                    gateway_host_key_path=str(tmp_path / "host_key"), **kw)


def test_parallel_visitors_up_to_the_limit_are_isolated_and_the_next_is_refused(published, managers, session_factory,
                                                                                tmp_path):
    limit = 5
    mgr = managers(max_active_sessions=limit)
    cfg = _cfg(tmp_path, gateway_max_connections_per_ip=limit + 1)

    async def scenario():
        gw = await start_gateway(mgr, cfg, session_factory, port=0)
        try:
            async def login(i):
                return await asyncssh.connect("127.0.0.1", gw.port, username=f"v{i}", password="pw", known_hosts=None,
                                              client_keys=None, agent_path=None)
            conns = await asyncio.gather(*(login(i) for i in range(limit)))
            # all five at once: each writes its own marker and reads the others' (none must be visible)
            await asyncio.gather(*(c.run(f"echo {i} > /tmp/marker-{i}", check=False) for i, c in enumerate(conns)))
            seen = await asyncio.gather(*(c.run("ls /tmp; hostname", check=False) for c in conns))
            extra = await login(99)                          # one more than the limit
            refused = await extra.run("echo hi", check=False)
            extra.close()
            for c in conns:
                c.close()
            await asyncio.sleep(1.5)
            again = await login(100)                         # a slot is free again
            ok = await again.run("echo back", check=False)
            again.close()
            await asyncio.sleep(1)
            return seen, refused, ok
        finally:
            await gw.close()

    seen, refused, ok = asyncio.run(scenario())
    hostnames = set()
    for i, out in enumerate(seen):
        lines = out.stdout.split()
        assert [x for x in lines if x.startswith("marker-")] == [f"marker-{i}"], f"visitor {i} saw: {lines}"
        hostnames.add(lines[-1])
    assert len(hostnames) == limit                                # five separate copies
    assert refused.exit_status != 0                               # turned away, not queued into someone's copy
    assert ok.stdout.strip() == "back"
    with session_factory() as v:
        assert any(e.event_type == "session.refused" for e in v.scalars(select(GatewayEvent)))
    assert mgr.driver.list_managed() == []                        # every copy removed after the visitors left


def test_restart_recovery_removes_orphans_and_closes_stranded_sessions(published, managers, session_factory):
    crashed = managers()
    a, b = crashed.create_session(), crashed.create_session()
    assert len(crashed.driver.list_managed()) == 2
    # the process dies: nothing is shut down. A new process starts and reconciles.
    fresh = managers()
    assert fresh.reconcile() == 2
    assert fresh.driver.list_managed() == []
    with session_factory() as v:
        for sid in (a.id, b.id):
            s = v.get(HoneypotSession, sid)
            assert s.status == "failed" and s.end_reason == "orchestrator restart"
    crashed._live.clear()  # its twins are gone; keep the fixture's shutdown from touching them


def test_reaper_ends_idle_and_overlong_visits_on_real_docker(published, managers, session_factory):
    mgr = managers(idle_ttl_s=5, session_ttl_s=10)
    idle, long = mgr.create_session(), mgr.create_session()
    mgr.execute(long.id, "echo active")
    now = datetime.now(UTC)
    with session_factory() as v:  # make `long` recently active, `idle` quiet for 6 s
        v.get(HoneypotSession, idle.id).last_activity_at = now - timedelta(seconds=6)
        v.get(HoneypotSession, long.id).last_activity_at = now
        v.commit()
    assert mgr.reap(now) == [idle.id]
    assert mgr.reap(now + timedelta(seconds=11)) == [long.id]
    with session_factory() as v:
        assert v.get(HoneypotSession, idle.id).end_reason == "idle timeout"
        assert v.get(HoneypotSession, long.id).end_reason == "ttl expired"
    assert mgr.driver.list_managed() == []
