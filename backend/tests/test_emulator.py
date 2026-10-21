"""The in-twin service emulators, exercised over real sockets with hand-written protocol clients."""
import asyncio
import hashlib
import importlib.util
import json
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

EMU_PATH = Path(__file__).resolve().parents[2] / "twin-image" / "emulator" / "hornet_emulator.py"
spec = importlib.util.spec_from_file_location("hornet_emulator", EMU_PATH)
emu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(emu)

TABLES = {
    "customers": {
        "columns": [{"name": "id", "type": "id"}, {"name": "email", "type": "email"}, {"name": "full_name", "type": "name"},
                    {"name": "active", "type": "bool"}],
        "rows": [{"id": 1, "email": "ana.lee@example.test", "full_name": "Ana Lee", "active": 1},
                 {"id": 2, "email": "bo.kim@example.test", "full_name": "Bo Kim", "active": 0},
                 {"id": 3, "email": "cy.ong@example.test", "full_name": "Cy Ong", "active": 1}],
    },
    "orders": {
        "columns": [{"name": "id", "type": "id"}, {"name": "customer_id", "type": "ref", "ref": "customers"},
                    {"name": "total", "type": "amount"}, {"name": "placed_at", "type": "timestamp"}],
        "rows": [{"id": 1, "customer_id": 1, "total": 1200, "placed_at": "2026-10-01 10:00:00"},
                 {"id": 2, "customer_id": 2, "total": 500, "placed_at": "2026-10-02 11:00:00"}],
    },
}
PG_PW, REDIS_PW = "Pg-fake-pass-123", "redis-fake-pass-456"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Running:
    def __init__(self, tmp_path):
        self.ports = {"http": free_port(), "pg": free_port(), "redis": free_port()}
        self.events = tmp_path / "events.jsonl"
        cfg = {
            "stores": {"db": {"tables": TABLES}},
            "services": [
                {"name": "web", "kind": "http", "bind": "127.0.0.1", "port": self.ports["http"], "store": "db", "server": "gunicorn"},
                {"name": "db", "kind": "postgres", "bind": "127.0.0.1", "port": self.ports["pg"], "store": "db",
                 "database": "shop", "user": "shop", "password": PG_PW},
                {"name": "cache", "kind": "redis", "bind": "127.0.0.1", "port": self.ports["redis"], "password": REDIS_PW,
                 "keys": {"session:1": "ana", "cache:customers:count": "3"}},
            ],
        }
        self.cfg_path = tmp_path / "services.json"
        self.cfg_path.write_text(json.dumps(cfg))
        import os

        os.environ["HORNET_EVENTS"] = str(self.events)
        self.loop = asyncio.new_event_loop()
        self.task = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        for _ in range(100):  # wait for the ready event
            if self.events.exists() and '"ready"' in self.events.read_text():
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("emulator did not start")

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.task = self.loop.create_task(emu.main(str(self.cfg_path)))
        try:
            self.loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            pass

    def stop(self):
        self.loop.call_soon_threadsafe(self.task.cancel)
        self.thread.join(5)

    def log(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()]


@pytest.fixture
def svc(tmp_path):
    r = Running(tmp_path)
    yield r
    r.stop()


# ---- HTTP ---------------------------------------------------------------------------------------
def http(port, method, path, body=None, headers=None, raw=None):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    data = json.dumps(body).encode() if body is not None else b""
    head = f"{method} {path} HTTP/1.1\r\nHost: web\r\nConnection: close\r\nContent-Length: {len(data)}\r\n"
    for k, v in (headers or {}).items():
        head += f"{k}: {v}\r\n"
    s.sendall(raw or (head.encode() + b"\r\n" + data))
    buf = b""
    while chunk := s.recv(65536):
        buf += chunk
    s.close()
    head_b, _, payload = buf.partition(b"\r\n\r\n")
    status = int(head_b.split()[1])
    return status, (json.loads(payload) if payload else None), head_b.decode()


def test_http_reads_filters_and_limits(svc):
    p = svc.ports["http"]
    assert http(p, "GET", "/health")[:2] == (200, {"status": "ok"})
    st, rows, head = http(p, "GET", "/api/customers")
    assert st == 200 and len(rows) == 3 and "Server: gunicorn" in head
    assert [r["id"] for r in http(p, "GET", "/api/customers?active=1")[1]] == [1, 3]
    assert len(http(p, "GET", "/api/customers?limit=2")[1]) == 2
    assert http(p, "GET", "/api/customers/2")[1]["email"] == "bo.kim@example.test"


def test_http_state_transitions_are_deterministic(svc):
    p = svc.ports["http"]
    st, row, _ = http(p, "POST", "/api/customers", {"email": "dee@example.test", "full_name": "Dee", "active": True})
    assert st == 201 and row["id"] == 4 and row["active"] == 1
    assert http(p, "GET", "/api/customers/4")[0] == 200
    assert http(p, "PATCH", "/api/customers/4", {"full_name": "Dee Dee"})[1]["full_name"] == "Dee Dee"
    assert http(p, "DELETE", "/api/customers/4")[0] == 204
    assert http(p, "GET", "/api/customers/4")[0] == 404
    assert http(p, "POST", "/api/customers", {"email": "x@example.test"})[1]["id"] == 4  # ids continue sequentially


@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [("POST", "/api/customers", {"nope": 1}, 422), ("POST", "/api/customers", [1, 2], 400), ("POST", "/api/orders", {"total": "abc"}, 422),
     ("GET", "/api/missing", None, 404), ("GET", "/etc/passwd", None, 404), ("GET", "/api/customers/99", None, 404),
     ("DELETE", "/api/customers", None, 405), ("PUT", "/api/customers/99", {"email": "a"}, 404)],
)
def test_http_errors_are_controlled(svc, method, path, body, status):
    assert http(svc.ports["http"], method, path, body)[0] == status


def test_http_rejects_garbage_and_oversized_bodies(svc):
    p = svc.ports["http"]
    assert http(p, "GET", "/", raw=b"NOT HTTP\r\n\r\n")[0] == 400
    assert http(p, "POST", "/api/customers", raw=b"POST /api/customers HTTP/1.1\r\nContent-Length: 999999\r\n\r\n")[0] == 413


def test_http_keep_alive_serves_several_requests(svc):
    s = socket.create_connection(("127.0.0.1", svc.ports["http"]), timeout=5)
    for _ in range(3):
        s.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
        assert b"200 OK" in s.recv(4096)
    s.close()


def test_http_requests_are_logged_as_events(svc):
    http(svc.ports["http"], "GET", "/api/customers", headers={"User-Agent": "sqlmap/1.7"})
    ev = [e for e in svc.log() if e.get("proto") == "http"]
    assert ev and ev[-1]["action"] == "GET /api/customers" and ev[-1]["status"] == 200 and ev[-1]["agent"] == "sqlmap/1.7"


# ---- Redis ----------------------------------------------------------------------------------------
class RedisClient:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.f = self.s.makefile("rb")

    def cmd(self, *args):
        self.s.sendall(f"*{len(args)}\r\n".encode() + b"".join(f"${len(a)}\r\n{a}\r\n".encode() for a in map(str, args)))
        return self.read()

    def read(self):
        line = self.f.readline().rstrip(b"\r\n")
        t, rest = line[:1], line[1:]
        if t in (b"+", b"-"):
            return (t.decode(), rest.decode())
        if t == b":":
            return int(rest)
        if t == b"$":
            n = int(rest)
            return None if n < 0 else self.f.read(n + 2)[:-2].decode()
        if t == b"*":
            return [self.read() for _ in range(int(rest))]
        raise AssertionError(line)


def test_redis_requires_auth_and_rejects_wrong_password(svc):
    c = RedisClient(svc.ports["redis"])
    assert c.cmd("GET", "session:1") == ("-", "NOAUTH Authentication required.")
    assert c.cmd("AUTH", "wrong")[1].startswith("WRONGPASS")
    assert c.cmd("AUTH", REDIS_PW) == ("+", "OK")
    assert c.cmd("GET", "session:1") == "ana"
    events = [e for e in svc.log() if e.get("proto") == "redis"]
    assert any(e["action"] == "AUTH" for e in events)
    assert REDIS_PW not in svc.events.read_text() and "wrong" not in svc.events.read_text()  # passwords never logged


def test_redis_commands(svc):
    c = RedisClient(svc.ports["redis"])
    c.cmd("AUTH", REDIS_PW)
    assert c.cmd("PING") == ("+", "PONG")
    assert c.cmd("SET", "k", "v") == ("+", "OK") and c.cmd("GET", "k") == "v"
    assert c.cmd("INCR", "n") == 1 and c.cmd("INCRBY", "n", 5) == 6
    assert sorted(c.cmd("KEYS", "session:*")) == ["session:1"]
    assert c.cmd("HSET", "h", "a", "1", "b", "2") == 2 and c.cmd("HGET", "h", "b") == "2"
    assert c.cmd("HGETALL", "h") == ["a", "1", "b", "2"]
    assert c.cmd("TYPE", "h") == ("+", "hash") and c.cmd("DEL", "k", "n") == 2
    assert c.cmd("GET", "h")[1].startswith("WRONGTYPE")
    assert "redis_version" in c.cmd("INFO")
    assert c.cmd("DBSIZE") == 3  # two seeded keys + the hash
    assert c.cmd("CONFIG", "SET", "dir", "/tmp")[0] == "-"        # cannot be used to write files
    assert c.cmd("SLAVEOF", "1.2.3.4", "6379")[0] == "-"          # nor to replicate from an attacker
    err = c.cmd("NOSUCH", "x")
    assert err[0] == "-" and "unknown command 'nosuch'" in err[1]
    assert any(e.get("unsupported") for e in svc.log() if e.get("proto") == "redis")


# ---- PostgreSQL -----------------------------------------------------------------------------------------
class PgClient:
    def __init__(self, port, user="shop", password=PG_PW, database="shop", ssl_probe=False):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.f = self.s.makefile("rb")
        if ssl_probe:
            self.s.sendall(struct.pack("!II", 8, 80877103))
            assert self.s.recv(1) == b"N"
        body = struct.pack("!I", 196608) + b"user\0" + user.encode() + b"\0database\0" + database.encode() + b"\0application_name\0psql\0\0"
        self.s.sendall(struct.pack("!I", len(body) + 4) + body)
        self.first = self.read()
        self.error = None
        if self.first[0] == b"R" and struct.unpack("!I", self.first[1][:4])[0] == 5:
            salt = self.first[1][4:8]
            inner = hashlib.md5((password + user).encode()).hexdigest()
            digest = b"md5" + hashlib.md5(inner.encode() + salt).hexdigest().encode() + b"\0"
            self.s.sendall(b"p" + struct.pack("!I", len(digest) + 4) + digest)
            msgs = self.until(b"Z")
            self.error = next((m[1] for m in msgs if m[0] == b"E"), None)
            self.params = {m[1].split(b"\0")[0].decode(): m[1].split(b"\0")[1].decode() for m in msgs if m[0] == b"S"}
        else:
            self.error = self.first[1] if self.first[0] == b"E" else None

    def read(self):
        t = self.f.read(1)
        if not t:
            return (b"", b"")
        n = struct.unpack("!I", self.f.read(4))[0]
        return t, self.f.read(n - 4)

    def until(self, stop):
        out = []
        while True:
            m = self.read()
            out.append(m)
            if m[0] in (stop, b""):
                return out
            if m[0] == b"E" and stop == b"Z" and not self.f.peek(1):
                return out

    def query(self, sql):
        self.s.sendall(b"Q" + struct.pack("!I", len(sql) + 5) + sql.encode() + b"\0")
        msgs = self.until(b"Z")
        cols, rows, tags, err = [], [], [], None
        for t, p in msgs:
            if t == b"T":
                n = struct.unpack("!H", p[:2])[0]
                pos, cols = 2, []
                for _ in range(n):
                    end = p.index(b"\0", pos)
                    cols.append(p[pos:end].decode())
                    pos = end + 1 + 18
            elif t == b"D":
                n = struct.unpack("!H", p[:2])[0]
                pos, row = 2, []
                for _ in range(n):
                    ln = struct.unpack("!i", p[pos:pos + 4])[0]
                    pos += 4
                    row.append(None if ln < 0 else p[pos:pos + ln].decode())
                    pos += max(ln, 0)
                rows.append(row)
            elif t == b"C":
                tags.append(p.rstrip(b"\0").decode())
            elif t == b"E":
                fields = {f[:1]: f[1:] for f in p.split(b"\0") if f}
                err = (fields[b"C"].decode(), fields[b"M"].decode())
        return cols, rows, tags, err


def test_pg_md5_authentication(svc):
    ok = PgClient(svc.ports["pg"], ssl_probe=True)  # psql probes for TLS first; we answer 'N' like a server without it
    assert ok.error is None and ok.params["server_version"].startswith("16.4") and ok.params["session_authorization"] == "shop"
    bad = PgClient(svc.ports["pg"], password="wrong")
    assert b"28P01" in bad.error and b"password authentication failed" in bad.error
    assert b"28P01" in PgClient(svc.ports["pg"], user="postgres").error          # unknown user looks the same
    assert b"3D000" in PgClient(svc.ports["pg"], database="nope").error
    log = [e for e in svc.log() if e.get("action") == "auth"]
    assert [e["status"] for e in log] == ["ok", "failed", "failed", "ok"] and PG_PW not in svc.events.read_text()


def test_pg_select_variants(svc):
    c = PgClient(svc.ports["pg"])
    cols, rows, tags, err = c.query("SELECT * FROM customers ORDER BY id DESC LIMIT 2")
    assert cols == ["id", "email", "full_name", "active"] and [r[0] for r in rows] == ["3", "2"] and tags == ["SELECT 2"] and err is None
    assert c.query("select count(*) from customers where active = true")[1] == [["2"]]
    assert c.query("SELECT email FROM public.customers WHERE full_name LIKE 'A%' AND id = 1")[1] == [["ana.lee@example.test"]]
    assert c.query("select id from customers where email ilike '%KIM%'")[1] == [["2"]]
    assert c.query("SELECT version()")[1][0][0].startswith("PostgreSQL 16.4")
    assert c.query("select current_user, current_database()")[1] == [["shop", "shop"]]
    assert c.query("select 1 as one, 'x'")[1] == [["1", "x"]]
    assert c.query("select id from customers where active is null")[1] == []
    cols, rows, _, _ = c.query("select c.id, c.email from customers c where c.id = 2")
    assert rows == [["2", "bo.kim@example.test"]]


def test_pg_catalog_views_attackers_use(svc):
    c = PgClient(svc.ports["pg"])
    names = [r[0] for r in c.query("select table_name from information_schema.tables where table_schema = 'public'")[1]]
    assert sorted(names) == ["customers", "orders"]
    cols = c.query("select column_name from information_schema.columns where table_name = 'orders' order by ordinal_position")[1]
    assert [r[0] for r in cols] == ["id", "customer_id", "total", "placed_at"]
    assert c.query("select tablename from pg_tables")[1] != []
    assert c.query("select rolname, rolsuper from pg_roles")[1] == [["shop", "f"]]
    psql_dt = ("SELECT n.nspname as \"Schema\", c.relname as \"Name\" FROM pg_catalog.pg_class c LEFT JOIN pg_catalog.pg_namespace n "
               "ON n.oid = c.relnamespace WHERE c.relkind IN ('r','p','') AND n.nspname <> 'pg_catalog' ORDER BY 1,2;")
    cols, rows, _, err = c.query(psql_dt)
    assert err is None and cols == ["Schema", "Name", "Type", "Owner"] and sorted(r[1] for r in rows) == ["customers", "orders"]


def test_pg_changes_are_visible_everywhere_and_deterministic(svc):
    c = PgClient(svc.ports["pg"])
    assert c.query("INSERT INTO customers (email, full_name, active) VALUES ('dee@example.test', 'Dee', true)")[2] == ["INSERT 0 1"]
    assert c.query("select id from customers where email = 'dee@example.test'")[1] == [["4"]]
    assert http(svc.ports["http"], "GET", "/api/customers/4")[1]["full_name"] == "Dee"      # same store as the HTTP API
    http(svc.ports["http"], "POST", "/api/customers", {"email": "eve@example.test"})
    assert c.query("select count(*) from customers")[1] == [["5"]]                          # and the other way round
    assert c.query("UPDATE customers SET full_name = 'D. Dee' WHERE id = 4")[2] == ["UPDATE 1"]
    assert c.query("DELETE FROM customers WHERE id > 3")[2] == ["DELETE 2"]
    assert c.query("insert into orders (customer_id, total) values (1, 10), (2, 20)")[2] == ["INSERT 0 2"]
    assert c.query("select id from orders order by id")[1] == [["1"], ["2"], ["3"], ["4"]]
    ret = c.query("insert into customers (email) values ('f@example.test') returning id")
    assert ret[1] == [["4"]] and ret[2] == ["INSERT 0 1"]


@pytest.mark.parametrize(
    ("sql", "code", "fragment"),
    [
        ("select * from nope", "42P01", 'relation "nope" does not exist'),
        ("select nope from customers", "42703", 'column "nope" does not exist'),
        ("select * from pg_shadow", "42501", "permission denied"),
        ("select * from pg_authid", "42501", "permission denied"),
        ("COPY customers TO PROGRAM 'id'", "42501", "external program"),
        ("COPY customers FROM '/etc/passwd'", "42501", "COPY"),
        ("select pg_read_file('/etc/passwd')", "42501", "pg_read_file"),
        ("insert into customers (id) values ('abc')", "22P02", "invalid input syntax for type integer"),
        ("insert into customers (zzz) values (1)", "42703", "does not exist"),
        ("insert into customers (id, email) values (1, 'dup@example.test')", "23505", "duplicate key"),
        ("gibberish here", "42601", 'syntax error at or near "gibberish"'),
        ("select 1 from", "42703", "does not exist"),
        ("show nonsense", "42704", "unrecognized configuration parameter"),
    ],
)
def test_pg_errors_use_real_postgres_codes(svc, sql, code, fragment):
    err = PgClient(svc.ports["pg"]).query(sql)[3]
    assert err is not None and err[0] == code and fragment in err[1], err


def test_pg_unsupported_statements_are_flagged_for_the_ai_layer(svc):
    PgClient(svc.ports["pg"]).query("gibberish here")
    assert any(e.get("unsupported") and e.get("proto") == "postgres" for e in svc.log())


def test_pg_multi_statement_strings_and_transactions(svc):
    c = PgClient(svc.ports["pg"])
    _, rows, tags, err = c.query("begin; select 1; select 2; commit")
    assert tags == ["BEGIN", "SELECT 1", "SELECT 1", "COMMIT"] and rows == [["1"], ["2"]] and err is None
    _, rows, tags, err = c.query("select 1; select * from nope; select 3")
    assert rows == [["1"]] and err[0] == "42P01"          # a failing statement aborts the rest, like PostgreSQL
    assert c.query("")[2] == [] and c.query("set search_path to public")[2] == ["SET"]
    assert c.query("show server_version")[1][0][0].startswith("16.4")


def test_pg_ddl_subset(svc):
    c = PgClient(svc.ports["pg"])
    assert c.query("create table notes (id serial primary key, body text)")[2] == ["CREATE TABLE"]
    assert c.query("insert into notes (body) values ('hi')")[2] == ["INSERT 0 1"]
    assert c.query("select body from notes")[1] == [["hi"]]
    assert c.query("create table notes (id int)")[3][0] == "42P07"
    assert c.query("truncate notes")[2] == ["TRUNCATE TABLE"] and c.query("drop table notes")[2] == ["DROP TABLE"]
    assert c.query("drop table notes")[3][0] == "42P01" and c.query("drop table if exists notes")[2] == ["DROP TABLE"]


def test_pg_extended_protocol_fails_cleanly_until_sync(svc):
    c = PgClient(svc.ports["pg"])
    payload = b"\0select 1\0\0\0"
    c.s.sendall(b"P" + struct.pack("!I", len(payload) + 4) + payload)
    assert c.read()[0] == b"E"
    c.s.sendall(b"S" + struct.pack("!I", 4))
    assert c.read()[0] == b"Z"
    assert c.query("select 1")[1] == [["1"]]          # the connection is still usable


def test_events_are_bounded_and_text_is_truncated(svc):
    http(svc.ports["http"], "GET", "/api/" + "x" * 5000)
    assert all(len(json.dumps(e)) < 5000 for e in svc.log())
    assert max(len(str(e.get("action", ""))) for e in svc.log()) <= emu.MAX_TEXT + 3


def test_every_event_satisfies_the_service_event_contract(svc):
    """docs/service-events.md: every line the emulator writes (all protocols, success and failure) fits the contract."""
    from app.orchestration.service_events import contract_problems, is_request

    http(svc.ports["http"], "GET", "/api/customers", headers={"User-Agent": "curl/8"})
    http(svc.ports["http"], "GET", "/no/such/route")
    http(svc.ports["http"], "POST", "/api/customers", body={"email": "x@example.test"})
    r = RedisClient(svc.ports["redis"])
    r.cmd("AUTH", REDIS_PW)
    r.cmd("GET", "session:1")
    r.cmd("NOSUCH", "x")
    pg = PgClient(svc.ports["pg"])
    pg.query("select * from customers")
    pg.query("gibberish here")
    pg.query("select * from no_such_table")

    events = svc.log()
    assert {e["proto"] for e in events if is_request(e)} == {"http", "postgres", "redis"}
    assert any(e.get("type") == "ready" for e in events)
    assert {json.dumps(e): contract_problems(e) for e in events if contract_problems(e)} == {}
    assert any(e.get("unsupported") is True for e in events if e.get("proto") == "postgres")
    assert any(e.get("unsupported") is True for e in events if e.get("proto") == "redis")


def test_contract_checker_rejects_malformed_events():
    from app.orchestration.service_events import contract_problems

    good = {"ts": "2026-10-18T00:00:00.000+00:00", "service": "db", "proto": "redis", "action": "GET", "status": "ok"}
    assert contract_problems(good) == []
    assert contract_problems({k: v for k, v in good.items() if k != "action"}) == ["missing action"]
    assert contract_problems({**good, "proto": "ftp"}) == ["unknown proto 'ftp'"]
    assert contract_problems({**good, "unsupported": "yes"}) == ["unsupported has type str"]
    assert contract_problems({**good, "surprise": 1}) == ["unexpected field surprise"]
    assert contract_problems({**good, "sql": "x" * 1000}) == ["sql is not truncated"]
    assert contract_problems({"type": "bogus"}) == ["unknown lifecycle line: 'bogus'"]


@pytest.mark.parametrize("method,path,unsupported", [
    ("GET", "/api/customers", False),           # served
    ("GET", "/api/customers/999", False),       # a missing row of a known table is a normal answer
    ("GET", "/admin/login", True),              # unknown route
    ("GET", "/api/invoices", True),             # unknown table
    ("PATCH", "/api/customers", True),          # method the emulator does not model (405)
    ("POST", "/api/customers", False),          # validation errors are normal answers (400)
])
def test_http_flags_unsupported_requests_for_the_ai_layer(svc, method, path, unsupported):
    http(svc.ports["http"], method, path, body={} if method in ("POST", "PATCH") else None)
    ev = [e for e in svc.log() if e.get("proto") == "http"][-1]
    assert ev["unsupported"] is unsupported, ev
