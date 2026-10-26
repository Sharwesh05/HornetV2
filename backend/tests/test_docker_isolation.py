"""Isolation tests against real hardened Docker twins. Skipped when Docker or the base image is missing.

These run hostile-looking commands inside the twin; the assertions are about what the *host and
other sessions* can still do afterwards.
"""
import time

import pytest

from app import services
from app.orchestration import session_manager as sm
from app.runtime.policy import ExecutionPolicy
from tests.helpers_runtime import TEST_RUNTIME

IMAGE = "hornet-twin-base:2"


def _docker_ready() -> bool:
    try:
        from app.runtime.docker_driver import HardenedDockerDriver

        d = HardenedDockerDriver()
        d.resolve_image(IMAGE)
        return d.ping()
    except Exception:
        return False


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_ready(), reason=f"Docker daemon or image {IMAGE} not available"),
]


@pytest.fixture
def published(db):
    snap = services.create_draft_snapshot(db, None)
    services.validate_snapshot(db, snap)
    services.publish_snapshot(db, snap)
    db.commit()
    return snap


@pytest.fixture
def make_mgr(session_factory):
    from tests.helpers_runtime import twin_driver

    created = []

    def _make(**policy):
        mgr = sm.SessionManager(twin_driver(), session_factory, ExecutionPolicy(**policy), IMAGE)
        created.append(mgr)
        return mgr

    yield _make
    for m in created:
        m.shutdown()
        m.reconcile()  # remove anything left over, even after a failed test


@pytest.fixture
def twin(published, make_mgr):
    mgr = make_mgr(timeout_ms=3000)
    sess = mgr.create_session()
    return mgr, sess


def run(twin, cmd):
    mgr, sess = twin
    return mgr.execute(sess.id, cmd)


def test_real_commands_return_real_output(twin):
    assert run(twin, "pwd").stdout.strip() == "/home/user"
    assert "uid=1000(user)" in run(twin, "id").stdout
    assert "Debian" in run(twin, "cat /etc/os-release").stdout
    ps = run(twin, "ps aux")
    assert ps.exit_code == 0 and "sleep infinity" in ps.stdout
    bad = run(twin, "ls /does/not/exist")
    assert bad.exit_code == 2 and "No such file" in bad.stderr and bad.stdout == ""
    assert run(twin, "exit 7").exit_code == 7


def test_no_network(twin):
    assert run(twin, "cat /proc/net/dev | tail -n +3 | cut -d: -f1 | tr -d ' '").stdout.split() == ["lo"]
    r = run(twin, "curl -m 2 -sS http://1.1.1.1/ 2>&1; echo rc=$?")
    assert "rc=0" not in r.stdout


def test_no_egress_by_any_protocol(twin):
    """Phase 7.1: default-deny egress, not only for TCP. Every probe must fail inside the twin."""
    assert "default" not in run(twin, "ip route show; ip -6 route show").stdout     # no route out at all
    probes = {
        "dns lookup": "getent hosts example.com",
        "dns to a public resolver (udp)": "timeout 3 bash -c 'echo x > /dev/udp/1.1.1.1/53'",
        "udp": "timeout 3 bash -c 'echo x > /dev/udp/203.0.113.10/9999'",
        "icmp": "ping -c 1 -W 2 1.1.1.1",
        "ipv6 tcp": "timeout 3 bash -c 'exec 3<>/dev/tcp/2606:4700:4700::1111/443'",
        "https by name": "curl -m 3 -sS https://example.com",
        "http by address": "curl -m 3 -sS http://93.184.215.14/",
    }
    for name, cmd in probes.items():
        r = run(twin, f"{cmd} >/dev/null 2>&1; echo rc=$?")
        assert "rc=0" not in r.stdout, f"egress worked: {name}"


def test_no_docker_socket_or_host_secrets(twin):
    assert run(twin, "test -e /var/run/docker.sock").exit_code != 0
    assert run(twin, "grep -c docker.sock /proc/mounts").stdout.strip() == "0"
    assert run(twin, "printenv | grep -i -E 'hornet|api_key|password'").stdout == ""
    assert run(twin, "find /mnt /host /run/desktop -mindepth 1 2>/dev/null | wc -l").stdout.strip() == "0"


def test_read_only_rootfs_and_writable_tmpfs_with_size_limit(twin):
    assert run(twin, "touch /etc/pwned").exit_code != 0
    assert run(twin, "touch /usr/bin/pwned").exit_code != 0
    ok = run(twin, "echo hi > /tmp/a && echo hi > ~/b && cat /tmp/a ~/b")
    assert ok.exit_code == 0
    assert sorted(c["path"] for c in ok.fs_changes) == ["/home/user/b", "/tmp/a"]
    big = run(twin, "dd if=/dev/zero of=/tmp/big bs=1M count=100 2>&1")
    assert "No space left" in big.stdout + big.stderr  # tmpfs is capped at 16 MB


def test_no_capabilities_no_privilege_escalation(twin):
    status = run(twin, "grep -E 'CapEff|CapBnd|NoNewPrivs' /proc/self/status").stdout
    assert "CapEff:\t0000000000000000" in status
    # gVisor does not report NoNewPrivs in /proc; test_docker_hardening checks it through the Docker API there
    assert "NoNewPrivs:\t1" in status or TEST_RUNTIME == "gvisor"
    assert run(twin, "find / -xdev -type f -perm /6000 2>/dev/null | wc -l").stdout.strip() == "0"
    assert run(twin, "su -c id").exit_code != 0
    assert run(twin, "mount -t tmpfs none /mnt").exit_code != 0


def test_attacker_cannot_touch_pid1(twin):
    assert run(twin, "ps -o user= -p 1").stdout.strip() != "user"
    assert run(twin, "kill -9 1").exit_code != 0
    assert "sleep infinity" in run(twin, "ps aux").stdout  # twin is still alive


def test_timeout_kills_command_and_twin_stays_usable(twin):
    started = time.monotonic()
    r = run(twin, "sleep 60")
    assert r.timed_out and time.monotonic() - started < 10
    assert run(twin, "echo alive").stdout.strip() == "alive"


def test_output_flood_is_capped_and_stopped(published, make_mgr):
    mgr = make_mgr(timeout_ms=5000, output_limit_bytes=4096)
    sess = mgr.create_session()
    started = time.monotonic()
    r = mgr.execute(sess.id, "yes AAAAAAAAAAAAAAAA")
    assert r.truncated and len(r.stdout) <= 4096
    assert time.monotonic() - started < 10
    # the runaway `yes` was killed, so the twin is not left burning CPU
    assert "yes AAAA" not in mgr.execute(sess.id, "ps aux").stdout


def test_fork_bomb_is_contained(published, make_mgr):
    mgr = make_mgr(timeout_ms=2000, pids=32)
    sess = mgr.create_session()
    started = time.monotonic()
    r = mgr.execute(sess.id, ":(){ :|:& };:")
    assert time.monotonic() - started < 15
    assert r.runtime_killed  # the saturated twin is detected and terminated, not left wedged
    with pytest.raises(sm.SessionNotUsable):
        mgr.execute(sess.id, "echo hi")
    # host is fine and the orchestrator can still tear down and start fresh twins
    mgr.destroy_session(sess.id)
    assert mgr.driver.list_managed() == []
    fresh = mgr.create_session()
    assert mgr.execute(fresh.id, "echo ok").stdout.strip() == "ok"


def test_memory_hog_is_limited(published, make_mgr):
    mgr = make_mgr(timeout_ms=8000, memory_mb=64)
    sess = mgr.create_session()
    started = time.monotonic()
    r = mgr.execute(sess.id, "x=$(head -c 400000000 /dev/zero | tr '\\0' a); echo done")
    assert time.monotonic() - started < 20
    assert "done" not in r.stdout  # killed by the memory limit before finishing


def test_sessions_are_isolated_from_each_other(published, make_mgr):
    mgr = make_mgr(timeout_ms=3000)
    a, b = mgr.create_session(), mgr.create_session()
    mgr.execute(a.id, "echo secret > /tmp/only-in-a; sleep 300 &")
    assert mgr.execute(b.id, "ls /tmp").stdout.strip() == ""
    assert "sleep 300" not in mgr.execute(b.id, "ps aux").stdout
    ha = mgr.execute(a.id, "hostname").stdout
    hb = mgr.execute(b.id, "hostname").stdout
    assert ha != hb and a.runtime_id != b.runtime_id


def test_filesystem_changes_are_observed(twin):
    run(twin, "echo one > /tmp/f")
    mod = run(twin, "echo two >> /tmp/f")
    assert [(c["path"], c["operation"]) for c in mod.fs_changes] == [("/tmp/f", "modified")]
    rm = run(twin, "rm /tmp/f")
    assert [(c["path"], c["operation"]) for c in rm.fs_changes] == [("/tmp/f", "deleted")]
    odd = run(twin, "touch '/tmp/we ird\ttab'")
    assert odd.fs_changes[0]["path"] == "/tmp/we ird\ttab"


def test_destroy_and_reconcile_leave_nothing_behind(published, make_mgr):
    mgr = make_mgr()
    s1, _ = mgr.create_session(), mgr.create_session()
    assert len(mgr.driver.list_managed()) == 2
    mgr.destroy_session(s1.id)
    assert len(mgr.driver.list_managed()) == 1
    assert not mgr.driver.is_running(s1.runtime_id)
    # simulate an orchestrator crash: a fresh manager must clean up the orphan
    fresh = make_mgr()
    assert fresh.reconcile() == 1
    assert mgr.driver.list_managed() == []
