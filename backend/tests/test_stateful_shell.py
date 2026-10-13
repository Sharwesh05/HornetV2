"""Persistent-shell state, graph linking and service events, at the unit level (in-memory runtime)."""
import uuid

import pytest
from sqlalchemy import select

from app import services
from app.models import ExecutionObservation, ExecutionRequest, ServiceProfile, SessionEvent, VirtualNode
from app.orchestration import session_manager as sm
from app.orchestration.linker import touched_paths
from app.orchestration.shell_state import ShellState
from app.runtime.policy import ExecutionPolicy
from tests.fakes import FakeDriver


# ---- ShellState ----------------------------------------------------------------------------------------
def test_wrapper_restores_state_and_uses_a_fresh_nonce():
    st = ShellState(cwd="/var/log", env={"WHO": "a b'c", "N": "1"})
    script, nonce = st.wrap("ls -la")
    lines = script.split("\n")
    assert lines[0].startswith("cd -- /var/log") and "export WHO='a b'\"'\"'c'" in lines
    assert "ls -la" in lines and lines[-1] == "exit $__hornet_rc" and nonce in script
    assert st.wrap("ls")[1] != nonce


def _marker(nonce, rc, cwd, env):
    return f"\x00HORNET:{nonce}\x00{rc}\x00{cwd}\x00" + "".join(f"{k}={v}\x00" for k, v in env.items())


def test_parse_strips_marker_and_updates_state():
    st = ShellState()
    out, rc = st.parse("hello\n" + _marker("abc", 3, "/tmp", {"A": "1", "PATH": "/x"}), "abc")
    assert out == "hello\n" and rc == 3 and st.cwd == "/tmp" and st.env == {"A": "1", "PATH": "/x"}


@pytest.mark.parametrize(
    "stdout",
    ["no marker at all", "x" + _marker("WRONG", 0, "/etc", {}), "x\x00HORNET:abc\x00notanumber\x00/etc\x00", "partial\x00HORNET:ab"],
)
def test_missing_or_forged_marker_leaves_state_alone(stdout):
    st = ShellState(cwd="/home/user", env={"K": "v"})
    out, rc = st.parse(stdout, "abc")
    assert rc is None and st.cwd == "/home/user" and st.env == {"K": "v"}


def test_parse_filters_dangerous_or_noisy_variables():
    st = ShellState()
    env = {"OK": "1", "_": "/bin/x", "PWD": "/", "SHLVL": "2", "BASH_ENV": "/x", "BASH_FUNC_f%%": "()", "1BAD": "x", "A-B": "x",
           "BIG": "v" * 5000, "IFS": "x"}
    st.parse(_marker("n", 0, "/tmp", env), "n")
    assert st.env == {"OK": "1"}


def test_parse_enforces_size_limits():
    st = ShellState()
    st.parse(_marker("n", 0, "/tmp", {f"V{i}": "x" * 100 for i in range(500)}), "n")
    assert len(st.env) <= 100
    st.parse(_marker("n", 0, "/" + "a" * 5000, {}), "n")
    assert st.cwd == "/tmp"                                     # an absurd cwd is ignored
    st.parse(_marker("n", 0, "relative/path", {}), "n")
    assert st.cwd == "/tmp"


def test_attacker_cannot_smuggle_commands_through_restored_state():
    st = ShellState(cwd="/tmp; rm -rf /", env={"X": "$(reboot)`id`; echo pwned"})
    script, _ = st.wrap("true")
    first, export = script.split("\n")[0], script.split("\n")[1]
    assert "'/tmp; rm -rf /'" in first and export == "export X='$(reboot)`id`; echo pwned'"   # quoted, inert


# ---- linker -----------------------------------------------------------------------------------------------
KNOWN = {"/", "/etc", "/etc/passwd", "/etc/redis/redis.conf", "/home/user", "/home/user/.pgpass", "/var/log", "/var/log/app/app.log"}


@pytest.mark.parametrize(
    ("cmd", "cwd", "expected"),
    [
        ("cat /etc/passwd", "/", ["/etc/passwd"]),
        ("cat passwd", "/etc", ["/etc/passwd"]),
        ("cat ../etc/passwd", "/var", ["/etc/passwd"]),
        ("cat ~/.pgpass", "/tmp", ["/home/user/.pgpass"]),
        ("cat < /etc/passwd > /tmp/out", "/", ["/etc/passwd"]),
        ("ls /var/log/app/*", "/", ["/var/log/app/app.log"]),
        ("grep -r secret /etc/redis", "/", ["/etc/redis/redis.conf"] if False else []),
        ("echo hi", "/", []),
        ("cat '/etc/passwd' \"/home/user/.pgpass\"", "/", ["/etc/passwd", "/home/user/.pgpass"]),
        ("cat 'unterminated /etc/passwd", "/", ["/etc/passwd"]),
        ("cat /etc/shadow /nonexistent", "/", []),
        ("--help -x", "/", []),
    ],
)
def test_linker_resolves_paths_against_known_nodes(cmd, cwd, expected):
    assert touched_paths(cmd, cwd, "/home/user", KNOWN) == expected


def test_linker_is_bounded():
    many = " ".join(f"/etc/passwd/{i}" for i in range(500)) + " /etc/passwd"
    assert touched_paths(many, "/", "/home/user", KNOWN) == []             # tokens beyond the cap are ignored
    assert len(touched_paths("ls " + "/var/log/app/* " * 50, "/", "/home/user", KNOWN)) <= 20


# ---- manager: stateful execution, links and service events -------------------------------------------------------
@pytest.fixture
def env(db, session_factory):
    snap = services.create_draft_snapshot(db, [
        ("/", None, "directory"), ("/etc", "/", "directory"), ("/etc/passwd", "/etc", "file"),
        ("/var", "/", "directory"), ("/var/log", "/var", "directory"),
        ("/services", "/", "directory"), ("/services/web", "/services", "service")])
    from app.models import ServiceProfile as SP

    db.add(SP(snapshot_id=snap.id, service_type="http-app", behavior_config={"name": "web", "loopback": "127.0.1.10"}))
    services.validate_snapshot(db, snap)
    services.publish_snapshot(db, snap)
    db.commit()
    driver = FakeDriver()
    return sm.SessionManager(driver, session_factory, ExecutionPolicy(), "hornet-twin-base:2"), driver, snap


def test_stateful_commands_share_directory_and_variables(env):
    mgr, driver, _ = env
    s = mgr.create_session()
    assert mgr.execute(s.id, "pwd", stateful=True).stdout == "/home/user\n"
    out = mgr.execute(s.id, "cd /var/log", stateful=True)
    assert out.cwd == "/var/log" and out.exit_code == 0
    mgr.execute(s.id, "export WHO=attacker", stateful=True)
    assert mgr.execute(s.id, "pwd", stateful=True).stdout == "/var/log\n"
    assert mgr.execute(s.id, "echo $WHO", stateful=True).stdout == "attacker\n"
    assert mgr.shell_context(s.id)[0] == "/var/log"


def test_stateless_commands_do_not_touch_shell_state(env):
    mgr, _, _ = env
    s = mgr.create_session()
    mgr.execute(s.id, "cd /var/log", stateful=True)
    assert mgr.execute(s.id, "echo hi").stdout == "hi\n"       # the admin console path
    assert mgr.shell_context(s.id)[0] == "/var/log"


def test_evidence_records_what_the_attacker_typed_not_the_wrapper(env, session_factory):
    mgr, _, _ = env
    s = mgr.create_session()
    mgr.execute(s.id, "echo secret-typed-command", "interactive_input", stateful=True)
    with session_factory() as v:
        req = v.scalars(select(ExecutionRequest)).one()
        import hashlib
        assert req.input_hash == hashlib.sha256(b"echo secret-typed-command").hexdigest() and req.operation == "interactive_input"
        stdout = v.scalars(select(ExecutionObservation)).one().result_metadata["stdout"]
        assert stdout == "secret-typed-command\n" and "HORNET" not in stdout and "\x00" not in stdout


def test_configure_shell_seeds_terminal_variables_safely(env):
    mgr, _, _ = env
    s = mgr.create_session()
    mgr.configure_shell(s.id, {"TERM": "xterm", "COLUMNS": "120", "BAD NAME": "x", "1X": "y", "LONG": "z" * 999})
    out = mgr.execute(s.id, "echo $COLUMNS", stateful=True)
    assert out.stdout == "120\n"
    with pytest.raises(sm.SessionNotUsable):
        mgr.shell_context(uuid.uuid4())


def test_commands_and_changes_are_linked_to_graph_nodes_as_observations(env, session_factory):
    mgr, _, snap = env
    s = mgr.create_session()
    mgr.execute(s.id, "touch /var/log", stateful=True)            # a path that exists in the base graph
    mgr.execute(s.id, "echo reading /etc/passwd", stateful=True)
    mgr.execute(s.id, "touch /tmp/brand-new", stateful=True)
    with session_factory() as v:
        ids = {n.virtual_path: n.id for n in v.scalars(select(VirtualNode).where(VirtualNode.snapshot_id == snap.id))}
        access = [e for e in v.scalars(select(SessionEvent)) if e.event_type == "resource_access"]
        assert {e.meta["path"]: e.node_id for e in access} == {"/var/log": ids["/var/log"], "/etc/passwd": ids["/etc/passwd"]}
        assert all(e.meta["basis"] == "command-argument" for e in access)       # an observation, not proof of a read
        changes = [e for e in v.scalars(select(SessionEvent)) if e.event_type == "fs_change"]
        by_path = {e.meta["path"]: e.node_id for e in changes}
        assert all(e.meta["basis"] == "filesystem-diff" for e in changes)
        assert by_path["/var/log"] == ids["/var/log"]                          # touching a base node links to it
        assert by_path["/tmp/brand-new"] is None                               # a new file is overlay, not a base node


def test_service_requests_are_stored_and_linked_to_the_service_node(env, session_factory):
    mgr, driver, snap = env
    s = mgr.create_session()
    assert driver.runtimes[s.runtime_id]["spec"].extra_hosts == {"web": "127.0.1.10"}      # names resolve to loopback aliases
    out = mgr.execute(s.id, "hit web", stateful=True)
    assert out.service_events == 1
    mgr.execute(s.id, "hit web", stateful=True)
    mgr.destroy_session(s.id)
    with session_factory() as v:
        node = v.scalars(select(VirtualNode).where(VirtualNode.virtual_path == "/services/web")).one()
        svc = [e for e in v.scalars(select(SessionEvent)) if e.event_type == "service.http"]
        assert len(svc) == 2 and all(e.node_id == node.id for e in svc)          # events are collected once, not twice
        assert svc[0].meta["action"] == "GET /api/customers" and "ts" not in svc[0].meta
        started = next(e for e in v.scalars(select(SessionEvent)) if e.event_type == "session.services")
        assert started.meta == {"status": "started", "services": ["web"]}


def test_a_snapshot_without_services_starts_no_emulators(db, session_factory):
    snap = services.create_draft_snapshot(db, None)
    services.validate_snapshot(db, snap)
    services.publish_snapshot(db, snap)
    db.commit()
    mgr = sm.SessionManager(FakeDriver(), session_factory, ExecutionPolicy(), "hornet-twin-base:2")
    mgr.create_session()
    with session_factory() as v:
        ev = next(e for e in v.scalars(select(SessionEvent)) if e.event_type == "session.services")
        assert ev.meta["status"] == "none"
        assert v.scalars(select(ServiceProfile)).all() == []


def test_a_command_still_succeeds_if_event_collection_breaks(env):
    mgr, driver, _ = env
    s = mgr.create_session()

    def boom(*a, **k):
        raise RuntimeError("collector down")

    driver.collect_service_events = boom
    assert mgr.execute(s.id, "echo ok", stateful=True).stdout == "ok\n"
