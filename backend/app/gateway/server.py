"""Attacker-facing SSH gateway.

One authenticated connection gets one twin (created when the first shell/exec channel opens, destroyed when
the connection closes). Every command goes through SessionManager.execute(stateful=True), so it is a
separate, recorded piece of evidence that still feels like one persistent shell.

Refused on purpose: SFTP/SCP and every other subsystem, TCP/UNIX/remote port forwarding, agent forwarding.
Anything refused is logged. This module never touches Docker and never shows an attacker an internal error.
"""
import asyncio
import logging
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import asyncssh
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.gateway.auth import AuthPolicy, ensure_host_key
from app.models import GatewayEvent
from app.orchestration import session_manager as sm

log = logging.getLogger("hornet.gateway")
MAX_AUTH_ATTEMPTS = 6


@dataclass(eq=False)  # identity semantics: instances are tracked in sets
class ConnState:
    conn: asyncssh.SSHServerConnection
    ip: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    started: float = field(default_factory=time.monotonic)
    attempts: int = 0
    username: str = ""
    session_id: uuid.UUID | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    counted: bool = False
    dead: bool = False
    cleaned: asyncio.Event = field(default_factory=asyncio.Event)  # set once the twin is destroyed


@dataclass
class Gateway:
    manager: sm.SessionManager
    settings: Settings
    db_factory: sessionmaker[Session]
    policy: AuthPolicy
    conns: dict = field(default_factory=dict)
    per_ip: Counter = field(default_factory=Counter)
    tasks: set = field(default_factory=set)
    pending: set = field(default_factory=set)  # connections whose cleanup has not finished yet
    acceptor: asyncssh.SSHAcceptor | None = None

    # ---- bookkeeping -------------------------------------------------------------------------------
    def spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _write(self, st: ConnState, event_type: str, meta: dict) -> None:
        with self.db_factory() as db:
            db.add(GatewayEvent(connection_id=st.id, session_id=st.session_id, source_ip=st.ip, event_type=event_type, meta=meta))
            db.commit()

    async def record(self, st: ConnState, event_type: str, **meta) -> None:
        try:
            await asyncio.to_thread(self._write, st, event_type, meta)
        except Exception:  # noqa: BLE001  (losing an event must never break a connection)
            log.exception("could not record gateway event %s", event_type)

    @property
    def port(self) -> int:
        return self.acceptor.get_addresses()[0][1]

    async def close(self) -> None:
        """Stop listening, hang up on everyone, and only return once every twin has been destroyed."""
        if self.acceptor is not None:
            self.acceptor.close()  # stop listening
        for st in list(self.conns.values()):
            st.conn.close()
        # connection_lost() fires after a close completes, so "no tasks yet" proves nothing: wait for each
        # connection's own cleanup to report completion instead
        waiting = [st.cleaned.wait() for st in list(self.pending)]
        if waiting:
            try:
                await asyncio.wait_for(asyncio.gather(*waiting), 60)
            except TimeoutError:
                log.error("gateway shutdown: %d connection(s) did not finish cleanup", len(self.pending))
        if self.acceptor is not None:
            await self.acceptor.wait_closed()
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    # ---- one twin per connection ----------------------------------------------------------------------
    async def ensure_session(self, st: ConnState) -> uuid.UUID | None:
        async with st.lock:
            if st.dead:
                return None
            if st.session_id is None:
                try:
                    sess = await asyncio.to_thread(self.manager.create_session, None, st.ip)
                except Exception as exc:  # noqa: BLE001  (limit reached, runtime down, nothing published ...)
                    await self.record(st, "session.refused", reason=type(exc).__name__)
                    st.dead = True
                    return None
                st.session_id = sess.id
                await self.record(st, "session.created", user=st.username)
            return st.session_id

    async def cleanup(self, st: ConnState) -> None:
        sid = st.session_id
        if sid is not None:
            try:
                await asyncio.to_thread(self.manager.destroy_session, sid, "disconnect")
            except Exception:  # noqa: BLE001
                log.exception("could not destroy session on disconnect")
        await self.record(st, "connection.closed", duration_s=round(time.monotonic() - st.started, 1),
                          attempts=st.attempts, had_session=sid is not None)
        self.pending.discard(st)
        st.cleaned.set()


class HornetServer(asyncssh.SSHServer):
    def __init__(self, gw: Gateway):
        self.gw = gw
        self.st: ConnState | None = None

    # -- connection lifecycle -------------------------------------------------------------------------
    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        peer = conn.get_extra_info("peername") or ("?", 0)
        self.st = st = ConnState(conn=conn, ip=str(peer[0]))
        gw = self.gw
        gw.conns[conn] = st
        gw.pending.add(st)
        if gw.per_ip[st.ip] >= gw.settings.gateway_max_connections_per_ip:
            gw.spawn(gw.record(st, "limit.rejected", reason="too many connections from this address"))
            st.dead = True
            conn.close()
            return
        gw.per_ip[st.ip] += 1
        st.counted = True
        gw.spawn(gw.record(st, "connection.opened", client_port=peer[1]))

    def connection_lost(self, exc: Exception | None) -> None:
        st, gw = self.st, self.gw
        if st is None:
            return
        gw.conns.pop(st.conn, None)
        if st.counted:
            gw.per_ip[st.ip] -= 1
        st.dead = True
        gw.spawn(gw.cleanup(st))

    # -- authentication -----------------------------------------------------------------------------
    def begin_auth(self, username: str) -> bool:
        self.st.username = username[:64]
        return True

    def password_auth_supported(self) -> bool:
        return True

    async def validate_password(self, username: str, password: str) -> bool:
        st, gw = self.st, self.gw
        st.attempts += 1
        ok = gw.policy.accepts(st.attempts, password)
        await gw.record(st, "auth.password", user=username[:64], success=ok, attempt=st.attempts, **gw.policy.describe(password))
        if not ok and st.attempts >= MAX_AUTH_ATTEMPTS:
            st.conn.close()
        return ok

    def public_key_auth_supported(self) -> bool:
        return True

    async def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        await self.gw.record(self.st, "auth.publickey", user=username[:64], fingerprint=key.get_fingerprint(), success=False)
        return False

    # -- things we never allow --------------------------------------------------------------------------
    def connection_requested(self, dest_host: str, dest_port: int, orig_host: str, orig_port: int):
        self.gw.spawn(self.gw.record(self.st, "forward.denied", kind="direct-tcpip", dest=f"{dest_host}:{dest_port}"))
        return False

    def server_requested(self, listen_host: str, listen_port: int):
        self.gw.spawn(self.gw.record(self.st, "forward.denied", kind="remote-forward", listen=f"{listen_host}:{listen_port}"))
        return False

    def unix_connection_requested(self, dest_path: str):
        self.gw.spawn(self.gw.record(self.st, "forward.denied", kind="unix", dest=dest_path[:100]))
        return False

    def unix_server_requested(self, listen_path: str):
        return False


# ---- the shell ------------------------------------------------------------------------------------------------
def _last_login(ip: str) -> str:
    when = datetime.now(UTC) - timedelta(days=2, hours=3, minutes=17)
    return f"Last login: {when:%a %b %d %H:%M:%S %Y} from 203.0.113.{(sum(map(ord, ip)) % 200) + 20}\n"


def _short_cwd(cwd: str) -> str:
    return "~" + cwd[len("/home/user"):] if cwd == "/home/user" or cwd.startswith("/home/user/") else cwd


async def _run(gw: Gateway, st: ConnState, process, sid: uuid.UUID, command: str, operation: str):
    """Run one command in the twin. Returns the outcome, or None if the session is gone."""
    try:
        out = await asyncio.to_thread(gw.manager.execute, sid, command, operation, stateful=True)
    except sm.InputRejected:
        process.stderr.write("-bash: command too long\n")
        return sm.ExecOutcome(uuid.uuid4(), 1, "", "", False, False, 0, False, [])
    except (sm.SessionNotUsable, sm.SessionNotFound):
        st.dead = True
        return None
    except Exception:  # noqa: BLE001
        log.exception("command failed")
        st.dead = True
        return None
    process.stdout.write(out.stdout)
    if out.stderr:
        process.stderr.write(out.stderr)
    if out.runtime_killed:
        st.dead = True  # a limit was exceeded and the twin was terminated: end the connection
    return out


async def handle_process(gw: Gateway, process: asyncssh.SSHServerProcess) -> None:
    st = gw.conns.get(process.channel.get_connection())
    if st is None or st.dead:
        process.exit(255)
        return
    if process.subsystem:  # sftp and friends
        await gw.record(st, "subsystem.denied", subsystem=str(process.subsystem)[:40])
        process.exit(1)
        return
    sid = await gw.ensure_session(st)
    if sid is None:
        process.exit(255)
        st.conn.close()
        return
    try:
        if process.command is not None:
            await _exec_mode(gw, st, process, sid)
        else:
            await _shell_mode(gw, st, process, sid)
    except (asyncssh.BreakReceived, asyncssh.SignalReceived, asyncssh.TerminalSizeChanged, ConnectionError):
        pass
    finally:
        if st.dead:
            st.conn.close()


async def _exec_mode(gw: Gateway, st: ConnState, process, sid: uuid.UUID) -> None:
    out = await _run(gw, st, process, sid, process.command, "exec_command")
    code = 255 if out is None or out.exit_code is None else out.exit_code
    await process.stdout.drain()
    process.exit(code & 0xFF)


async def _shell_mode(gw: Gateway, st: ConnState, process, sid: uuid.UUID) -> None:
    mgr = gw.manager
    pty = process.get_terminal_type() is not None
    if pty:
        cols, rows, _, _ = process.get_terminal_size()
        await asyncio.to_thread(mgr.configure_shell, sid, {"TERM": process.get_terminal_type() or "xterm",
                                                            "COLUMNS": str(cols or 80), "LINES": str(rows or 24)})
        motd = await _run(gw, st, process, sid, "cat /etc/motd 2>/dev/null", "login_banner")
        if motd is None:
            return
        process.stdout.write("\n" + _last_login(st.ip))
    idle = gw.manager.policy.idle_ttl_s
    while not st.dead:
        if pty:
            cwd, host = mgr.shell_context(sid)
            process.stdout.write(f"user@{host}:{_short_cwd(cwd)}$ ")
        try:
            line = await asyncio.wait_for(process.stdin.readline(), idle)
        except TimeoutError:
            process.stdout.write("\ntimed out waiting for input: auto-logout\n")
            break
        except (asyncssh.TerminalSizeChanged, asyncssh.BreakReceived, asyncssh.SignalReceived):
            if pty:
                process.stdout.write("^C\n")
            continue
        if not line:  # EOF: client closed its end (ssh -T pipes, Ctrl-D)
            break
        command = line.strip()
        if not command:
            continue
        if command in ("exit", "logout"):
            break
        if await _run(gw, st, process, sid, command, "interactive_input" if pty else "piped_input") is None:
            break
    if pty:
        process.stdout.write("logout\n")
    process.exit(0)


async def start_gateway(manager: sm.SessionManager, settings: Settings, db_factory: sessionmaker[Session],
                        port: int | None = None) -> Gateway:
    """Start listening. `port=0` picks a free port (tests)."""
    policy = AuthPolicy(settings.gateway_accept_after, settings.weak_passwords(), settings.gateway_log_passwords)
    gw = Gateway(manager=manager, settings=settings, db_factory=db_factory, policy=policy)

    async def process_factory(process):
        await handle_process(gw, process)

    gw.acceptor = await asyncssh.create_server(
        lambda: HornetServer(gw),
        settings.gateway_host,
        settings.gateway_port if port is None else port,
        server_host_keys=[ensure_host_key(settings.gateway_host_key_path)],
        server_version=settings.gateway_server_version,
        process_factory=process_factory,
        login_timeout=settings.gateway_login_timeout_s,
        encoding="utf-8",
        errors="replace",
        allow_scp=False,
        sftp_factory=None,
        keepalive_interval=30,
        agent_forwarding=False,
        x11_forwarding=False,
    )
    log.info("SSH gateway listening on %s:%s", settings.gateway_host, gw.port)
    return gw
