"""Service emulators that run INSIDE a twin (standard library only, Python 3.11 compatible).

One small process serves the fake services of the observed environment, each on its own loopback address:
  * http      a JSON API over the environment's tables (GET/POST/PUT/PATCH/DELETE)
  * postgres  PostgreSQL wire protocol (md5 auth, simple queries, a useful SQL subset)
  * redis     RESP protocol (strings, hashes, AUTH)

All services share one in-memory store per database, so a POST to the API is visible to `psql`, and the
state dies with the twin (nothing is shared between sessions). Behaviour is deterministic: fixtures come
from the build, ids are sequential, errors use real PostgreSQL / Redis messages. Anything outside the
supported subset gets a controlled error and an `unsupported` event, which is where the AI response
engine (Phase 5) plugs in later.

Every request is appended as one JSON line to an events file that only the system user can read; the
orchestrator collects it. Attacker-supplied text is truncated and passwords are never stored.
"""
import asyncio
import fnmatch
import hashlib
import json
import os
import re
import struct
import sys
import time
from datetime import UTC, datetime

MAX_TEXT = 400
MAX_BODY = 64 * 1024
MAX_EVENTS_BYTES = 2_000_000
IDLE_TIMEOUT = 120
INT_TYPES = {"id", "int", "amount", "ref"}


# ---- events ------------------------------------------------------------------------------------
class EventLog:
    def __init__(self, path):
        self.path, self.size, self.dropped = path, 0, 0
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.fh = open(path, "a", encoding="utf-8", buffering=1)

    def emit(self, **fields):
        for k, v in list(fields.items()):
            if isinstance(v, str) and len(v) > MAX_TEXT:
                fields[k] = v[:MAX_TEXT] + "..."
        fields["ts"] = datetime.now(UTC).isoformat(timespec="milliseconds")
        line = json.dumps(fields, ensure_ascii=False, default=str) + "\n"
        if self.size + len(line) > MAX_EVENTS_BYTES:
            self.dropped += 1
            return
        self.size += len(line)
        self.fh.write(line)


# ---- the shared data store ---------------------------------------------------------------------
class SqlError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def coerce(ctype, value, column="value"):
    """Convert a Python/JSON/SQL literal to the column's type, or raise SqlError(22P02)."""
    if value is None:
        return None
    try:
        if ctype in INT_TYPES:
            if isinstance(value, bool):
                raise ValueError
            return int(value)
        if ctype == "bool":
            if isinstance(value, str):
                low = value.lower()
                if low in ("t", "true", "1", "yes", "on"):
                    return 1
                if low in ("f", "false", "0", "no", "off"):
                    return 0
                raise ValueError
            return 1 if value else 0
        return str(value)
    except (ValueError, TypeError):
        kind = "integer" if ctype in INT_TYPES else "boolean"
        raise SqlError("22P02", f'invalid input syntax for type {kind}: "{str(value)[:40]}"') from None


class Store:
    def __init__(self, tables):
        self.tables = {n: {"columns": t["columns"], "rows": [dict(r) for r in t["rows"]]} for n, t in tables.items()}

    def table(self, name):
        t = self.tables.get(name)
        if t is None:
            raise SqlError("42P01", f'relation "{name}" does not exist')
        return t

    def column(self, table, name):
        if isinstance(table, str):
            table = self.table(table)
        for c in table["columns"]:
            if c["name"] == name:
                return c
        raise SqlError("42703", f'column "{name}" does not exist')

    def insert(self, name, values):
        t = self.table(name)
        cols = {c["name"]: c for c in t["columns"]}
        for k in values:
            if k not in cols:
                raise SqlError("42703", f'column "{k}" of relation "{name}" does not exist')
        row = {}
        for c in t["columns"]:
            if c["name"] in values:
                row[c["name"]] = coerce(c["type"], values[c["name"]], c["name"])
            elif c["type"] == "id":
                row[c["name"]] = max((r[c["name"]] for r in t["rows"]), default=0) + 1
            else:
                row[c["name"]] = None
        idc = next((c["name"] for c in t["columns"] if c["type"] == "id"), None)
        if idc and any(r[idc] == row[idc] for r in t["rows"]):
            raise SqlError("23505", f'duplicate key value violates unique constraint "{name}_pkey"')
        t["rows"].append(row)
        return row

    def match(self, name, preds):
        t = self.table(name)
        return [r for r in t["rows"] if all(p(r) for p in preds)]


# ---- HTTP -------------------------------------------------------------------------------------------
REASONS = {200: "OK", 201: "Created", 204: "No Content", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
           413: "Payload Too Large", 422: "Unprocessable Entity", 500: "Internal Server Error"}


def http_response(status, body, server, ctype="application/json"):
    data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body, default=str).encode())
    head = (f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}\r\nServer: {server}\r\nContent-Type: {ctype}\r\n"
            f"Content-Length: {len(data)}\r\nConnection: keep-alive\r\n\r\n").encode()
    return head + data


def handle_api(store, method, path, query, body):
    """Return (status, payload). Pure function over the store: deterministic."""
    parts = [p for p in path.split("/") if p]
    if method == "GET" and not parts:
        return 200, {"status": "ok"}
    if parts == ["health"] and method in ("GET", "HEAD"):
        return 200, {"status": "ok"}
    if not parts or parts[0] != "api" or len(parts) not in (2, 3):
        return 404, {"error": "not found"}
    name = parts[1]
    if name not in store.tables:
        return 404, {"error": "not found"}
    t, cols = store.tables[name], {c["name"]: c for c in store.tables[name]["columns"]}
    idc = next(c["name"] for c in t["columns"] if c["type"] == "id")
    if len(parts) == 2:
        if method == "GET":
            rows = t["rows"]
            for k, v in query.items():
                if k in cols:
                    rows = [r for r in rows if str(r.get(k)) == v]
            limit = int(query["limit"]) if query.get("limit", "").isdigit() else 100
            return 200, rows[: min(limit, 500)]
        if method == "POST":
            if not isinstance(body, dict):
                return 400, {"error": "JSON object expected"}
            unknown = sorted(set(body) - set(cols))
            if unknown:
                return 422, {"error": "unknown fields", "fields": unknown}
            try:
                return 201, store.insert(name, {k: v for k, v in body.items() if k != idc})
            except SqlError as e:
                return 422, {"error": e.message}
        return 405, {"error": "method not allowed"}
    ident = parts[2]
    rows = [r for r in t["rows"] if str(r[idc]) == ident]
    if method == "GET":
        return (200, rows[0]) if rows else (404, {"error": "not found"})
    if not rows:
        return 404, {"error": "not found"}
    if method == "DELETE":
        t["rows"].remove(rows[0])
        return 204, None
    if method in ("PUT", "PATCH"):
        if not isinstance(body, dict):
            return 400, {"error": "JSON object expected"}
        unknown = sorted(set(body) - set(cols))
        if unknown:
            return 422, {"error": "unknown fields", "fields": unknown}
        try:
            for k, v in body.items():
                if k != idc:
                    rows[0][k] = coerce(cols[k]["type"], v, k)
        except SqlError as e:
            return 422, {"error": e.message}
        return 200, rows[0]
    return 405, {"error": "method not allowed"}


async def serve_http(reader, writer, svc, store, log):
    peer = writer.get_extra_info("peername") or ("?", 0)
    server = svc.get("server", "nginx/1.22.1")
    try:
        for _ in range(100):  # keep-alive, bounded
            try:
                line = await asyncio.wait_for(reader.readuntil(b"\r\n"), IDLE_TIMEOUT)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
                return
            try:
                method, target, _version = line.decode("latin-1").strip().split(" ", 2)
            except ValueError:
                writer.write(http_response(400, {"error": "bad request"}, server))
                return
            headers = {}
            for _h in range(100):
                hline = await asyncio.wait_for(reader.readuntil(b"\r\n"), 15)
                if hline == b"\r\n":
                    break
                k, _, v = hline.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            length = int(headers.get("content-length", "0") or 0) if headers.get("content-length", "0").isdigit() else 0
            if length > MAX_BODY:
                writer.write(http_response(413, {"error": "payload too large"}, server))
                return
            raw = await asyncio.wait_for(reader.readexactly(length), 15) if length else b""
            path, _, qs = target.partition("?")
            query = dict(p.split("=", 1) if "=" in p else (p, "") for p in qs.split("&") if p)
            body = None
            if raw:
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
            status, payload = handle_api(store, method.upper(), path, query, body)
            log.emit(service=svc["name"], proto="http", action=f"{method} {target}", status=status,
                     remote=peer[0], agent=headers.get("user-agent", ""), body=raw[:200].decode("utf-8", "replace"))
            writer.write(http_response(status, payload, server))
            await writer.drain()
            if headers.get("connection", "").lower() == "close":
                return
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
        pass
    finally:
        writer.close()


# ---- Redis ----------------------------------------------------------------------------------------------
class RedisState:
    def __init__(self, keys):
        self.data = {k: ("string", v) for k, v in (keys or {}).items()}


async def resp_read(reader):
    """Read one command: RESP array of bulk strings, or an inline command. Returns list[bytes] or None."""
    first = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT)
    if not first:
        return None
    first = first.rstrip(b"\r\n")
    if not first:
        return []
    if first[:1] != b"*":
        return first.split()
    n = int(first[1:])
    if n > 1024:
        raise ValueError("too many args")
    out = []
    for _ in range(n):
        head = (await reader.readline()).rstrip(b"\r\n")
        if head[:1] != b"$":
            raise ValueError("protocol error")
        size = int(head[1:])
        if size > MAX_BODY:
            raise ValueError("bulk too large")
        out.append(await reader.readexactly(size + 2))
        out[-1] = out[-1][:-2]
    return out


def resp(v):
    if v is None:
        return b"$-1\r\n"
    if isinstance(v, bool):
        return b":1\r\n" if v else b":0\r\n"
    if isinstance(v, int):
        return f":{v}\r\n".encode()
    if isinstance(v, tuple):  # ("ok", text) / ("err", text)
        return (b"+" if v[0] == "ok" else b"-") + v[1].encode() + b"\r\n"
    if isinstance(v, list):
        return f"*{len(v)}\r\n".encode() + b"".join(resp(x) for x in v)
    b = v if isinstance(v, bytes) else str(v).encode()
    return b"$" + str(len(b)).encode() + b"\r\n" + b + b"\r\n"


def redis_info(svc, st):
    return ("# Server\r\nredis_version:7.2.4\r\nredis_mode:standalone\r\nos:Linux x86_64\r\ntcp_port:%d\r\n"
            "uptime_in_seconds:86412\r\n# Clients\r\nconnected_clients:3\r\n# Memory\r\nused_memory_human:1.02M\r\n"
            "# Keyspace\r\ndb0:keys=%d,expires=0,avg_ttl=0\r\n" % (svc["port"], len(st.data)))


def redis_exec(args, st, svc, authed):
    cmd = args[0].decode("latin-1").upper()
    a = [x.decode("utf-8", "replace") for x in args[1:]]
    pw = svc.get("password")
    if cmd == "AUTH":
        given = a[-1] if a else ""
        if pw and given == pw:
            return ("ok", "OK"), True
        return ("err", "WRONGPASS invalid username-password pair or user is disabled."), authed
    if cmd == "QUIT":
        return ("ok", "OK"), authed
    if pw and not authed:
        return ("err", "NOAUTH Authentication required."), authed
    d = st.data
    if cmd == "PING":
        return (a[0] if a else ("ok", "PONG")), authed
    if cmd == "ECHO" and a:
        return a[0], authed
    if cmd == "SET" and len(a) >= 2:
        d[a[0]] = ("string", a[1])
        return ("ok", "OK"), authed
    if cmd == "GET" and a:
        v = d.get(a[0])
        if v and v[0] != "string":
            return ("err", "WRONGTYPE Operation against a key holding the wrong kind of value"), authed
        return (v[1] if v else None), authed
    if cmd in ("DEL", "UNLINK"):
        return sum(1 for k in a if d.pop(k, None) is not None), authed
    if cmd == "EXISTS":
        return sum(1 for k in a if k in d), authed
    if cmd == "KEYS":
        return sorted(k for k in d if fnmatch.fnmatchcase(k, a[0] if a else "*")), authed
    if cmd == "DBSIZE":
        return len(d), authed
    if cmd in ("INCR", "DECR", "INCRBY", "DECRBY") and a:
        step = int(a[1]) if len(a) > 1 and a[1].lstrip("-").isdigit() else 1
        cur = d.get(a[0], ("string", "0"))[1]
        if not str(cur).lstrip("-").isdigit():
            return ("err", "ERR value is not an integer or out of range"), authed
        new = int(cur) + (step if cmd in ("INCR", "INCRBY") else -step)
        d[a[0]] = ("string", str(new))
        return new, authed
    if cmd == "MGET":
        return [d[k][1] if k in d and d[k][0] == "string" else None for k in a], authed
    if cmd == "MSET":
        for i in range(0, len(a) - 1, 2):
            d[a[i]] = ("string", a[i + 1])
        return ("ok", "OK"), authed
    if cmd == "TYPE" and a:
        return ("ok", d[a[0]][0] if a[0] in d else "none"), authed
    if cmd in ("EXPIRE", "PEXPIRE", "PERSIST"):
        return (1 if a and a[0] in d else 0), authed
    if cmd == "TTL":
        return (-1 if a and a[0] in d else -2), authed
    if cmd == "HSET" and len(a) >= 3:
        h = d.setdefault(a[0], ("hash", {}))[1]
        n = 0
        for i in range(1, len(a) - 1, 2):
            n += a[i] not in h
            h[a[i]] = a[i + 1]
        return n, authed
    if cmd == "HGET" and len(a) == 2:
        v = d.get(a[0])
        return (v[1].get(a[1]) if v and v[0] == "hash" else None), authed
    if cmd == "HGETALL" and a:
        v = d.get(a[0])
        return ([x for kv in v[1].items() for x in kv] if v and v[0] == "hash" else []), authed
    if cmd in ("FLUSHALL", "FLUSHDB"):
        d.clear()
        return ("ok", "OK"), authed
    if cmd == "SELECT":
        return ("ok", "OK"), authed
    if cmd == "INFO":
        return redis_info(svc, st), authed
    if cmd == "COMMAND":
        return [], authed
    if cmd == "CONFIG":
        if a and a[0].upper() == "GET":
            return [], authed
        return ("err", "ERR CONFIG SET is disabled"), authed
    if cmd in ("CLIENT", "HELLO", "SUBSCRIBE", "MONITOR", "SAVE", "BGSAVE", "SLAVEOF", "REPLICAOF", "MODULE", "EVAL"):
        return ("err", "ERR unknown or disabled command '%s'" % cmd.lower()), authed
    return ("err", "ERR unknown command '%s', with args beginning with: %s" % (cmd.lower(), " ".join("'%s'" % x[:20] for x in a[:3]))), authed


async def serve_redis(reader, writer, svc, st, log):
    peer = writer.get_extra_info("peername") or ("?", 0)
    authed = not svc.get("password")
    try:
        while True:
            try:
                args = await resp_read(reader)
            except (ValueError, asyncio.IncompleteReadError):
                writer.write(resp(("err", "ERR Protocol error")))
                return
            if args is None:
                return
            if not args:
                continue
            result, authed2 = redis_exec(args, st, svc, authed)
            cmd = args[0].decode("latin-1").upper()
            unsupported = isinstance(result, tuple) and result[0] == "err" and "unknown" in result[1]
            log.emit(service=svc["name"], proto="redis", action=cmd + (" " + " ".join(x.decode("utf-8", "replace")[:40] for x in args[1:4]) if cmd != "AUTH" else ""),
                     status="error" if isinstance(result, tuple) and result[0] == "err" else "ok",
                     remote=peer[0], auth="ok" if cmd == "AUTH" and authed2 else None, unsupported=unsupported)
            authed = authed2
            writer.write(resp(result))
            await writer.drain()
            if cmd == "QUIT":
                return
    except (asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()


# ---- PostgreSQL ------------------------------------------------------------------------------------------
PG_VERSION = "PostgreSQL 16.4 (Debian 16.4-1.pgdg120+2) on x86_64-pc-linux-gnu, compiled by gcc (Debian 12.2.0-14) 12.2.0, 64-bit"
OID = {"int": 23, "bool": 16, "timestamp": 1114, "text": 25}


def pg_msg(t, payload=b""):
    return t + struct.pack("!I", len(payload) + 4) + payload


def pg_error(code, message, fatal=False):
    sev = b"FATAL" if fatal else b"ERROR"
    body = b"S" + sev + b"\0V" + sev + b"\0C" + code.encode() + b"\0M" + message.encode() + b"\0\0"
    return pg_msg(b"E", body)


def pg_rowdesc(cols):
    out = struct.pack("!H", len(cols))
    for name, oid in cols:
        out += name.encode() + b"\0" + struct.pack("!IhIhih", 0, 0, oid, -1 if oid == 25 else 4, -1, 0)
    return pg_msg(b"T", out)


def pg_datarow(values):
    out = struct.pack("!H", len(values))
    for v in values:
        if v is None:
            out += struct.pack("!i", -1)
        else:
            b = str(v).encode()
            out += struct.pack("!i", len(b)) + b
    return pg_msg(b"D", out)


def sql_type_oid(ctype):
    return OID["int"] if ctype in INT_TYPES else OID["bool"] if ctype == "bool" else OID["timestamp"] if ctype == "timestamp" else OID["text"]


def split_statements(sql):
    out, cur, q = [], [], None
    for ch in sql:
        if q:
            cur.append(ch)
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
            cur.append(ch)
        elif ch == ";":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [s.strip() for s in out if s.strip()]


def split_top(s, sep=","):
    out, cur, q, depth = [], [], None, 0
    for ch in s:
        if q:
            cur.append(ch)
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == sep and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return [x for x in out if x != ""]


def literal(tok):
    t = tok.strip()
    if t.lower() == "null":
        return None
    if t.lower() in ("true", "false"):
        return t.lower() == "true"
    if len(t) >= 2 and t[0] == "'" and t[-1] == "'":
        return t[1:-1].replace("''", "'")
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d+\.\d+", t):
        return float(t)
    raise SqlError("42601", 'syntax error at or near "%s"' % t[:20])


_PRED = re.compile(r"^(?P<col>[a-z_][a-z0-9_.]*)\s*(?P<op>=|!=|<>|<=|>=|<|>|\s+i?like\s+|\s+is\s+not\s+null|\s+is\s+null)\s*(?P<val>.*)$", re.I | re.S)


def build_preds(store, table, where):
    preds = []
    if not where:
        return preds
    for clause in re.split(r"\s+and\s+", where, flags=re.I):
        m = _PRED.match(clause.strip())
        if not m:
            raise SqlError("42601", 'syntax error at or near "%s"' % clause.strip()[:20])
        col, op = m.group("col").split(".")[-1], re.sub(r"\s+", " ", m.group("op").strip().lower())
        c = store.column(table, col)
        if op == "is null":
            preds.append(lambda r, col=col: r.get(col) is None)
            continue
        if op == "is not null":
            preds.append(lambda r, col=col: r.get(col) is not None)
            continue
        val = coerce(c["type"], literal(m.group("val")))
        if op in ("like", "ilike"):
            rx = re.compile("^" + re.escape(str(val)).replace("%", ".*").replace("_", ".") + "$", re.I if op == "ilike" else 0)
            preds.append(lambda r, col=col, rx=rx: r.get(col) is not None and bool(rx.match(str(r[col]))))
        else:
            cmp = {"=": lambda a, b: a == b, "!=": lambda a, b: a != b, "<>": lambda a, b: a != b, "<": lambda a, b: a < b,
                   ">": lambda a, b: a > b, "<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b}[op]
            preds.append(lambda r, col=col, cmp=cmp, val=val: r.get(col) is not None and cmp(r[col], val))
    return preds


class PgEngine:
    def __init__(self, store, svc):
        self.store, self.svc = store, svc

    def virtual(self, name):
        """information_schema / pg_catalog views, as tables the normal SELECT code can read."""
        s, user, rows = self.store, self.svc["user"], []
        if name in ("information_schema.tables", "pg_catalog.pg_tables", "pg_tables"):
            if name == "information_schema.tables":
                cols = [("table_catalog", "text"), ("table_schema", "text"), ("table_name", "text"), ("table_type", "text")]
                rows = [{"table_catalog": self.svc["database"], "table_schema": "public", "table_name": n, "table_type": "BASE TABLE"} for n in s.tables]
            else:
                cols = [("schemaname", "text"), ("tablename", "text"), ("tableowner", "text")]
                rows = [{"schemaname": "public", "tablename": n, "tableowner": user} for n in s.tables]
        elif name == "information_schema.columns":
            cols = [("table_schema", "text"), ("table_name", "text"), ("column_name", "text"), ("ordinal_position", "int"), ("data_type", "text")]
            for n, t in s.tables.items():
                for i, c in enumerate(t["columns"], 1):
                    dt = "integer" if c["type"] in INT_TYPES else "boolean" if c["type"] == "bool" else "timestamp without time zone" if c["type"] == "timestamp" else "text"
                    rows.append({"table_schema": "public", "table_name": n, "column_name": c["name"], "ordinal_position": i, "data_type": dt})
        elif name in ("pg_catalog.pg_roles", "pg_roles", "pg_catalog.pg_user", "pg_user"):
            cols = [("rolname", "text"), ("rolsuper", "bool"), ("rolcanlogin", "bool")]
            rows = [{"rolname": user, "rolsuper": 0, "rolcanlogin": 1}]
        elif name in ("pg_catalog.pg_database", "pg_database"):
            cols = [("datname", "text")]
            rows = [{"datname": self.svc["database"]}, {"datname": "postgres"}]
        else:
            return None
        return {"columns": [{"name": n, "type": t} for n, t in cols], "rows": rows}

    # each result is ("rows", cols, rows, tag) | ("cmd", tag) | raises SqlError
    def run(self, stmt):
        s = re.sub(r"--.*$", "", stmt, flags=re.M).strip()
        low = re.sub(r"\s+", " ", s.lower())
        if not low:
            return ("empty",)
        if "pg_catalog.pg_class" in low and "relkind" in low:  # psql \dt, \d
            rows = [[("public"), n, "table", self.svc["user"]] for n in self.store.tables]
            return ("rows", [("Schema", 25), ("Name", 25), ("Type", 25), ("Owner", 25)], rows, f"SELECT {len(rows)}")
        if "pg_catalog.pg_database" in low and "from" in low and "datname" in low and "where" not in low.replace("datallowconn", ""):
            db = self.svc["database"]
            return ("rows", [("Name", 25), ("Owner", 25), ("Encoding", 25), ("Collate", 25), ("Ctype", 25), ("Access privileges", 25)],
                    [[db, self.svc["user"], "UTF8", "en_US.utf8", "en_US.utf8", None], ["postgres", "postgres", "UTF8", "en_US.utf8", "en_US.utf8", None]], "SELECT 2")
        if "pg_catalog.pg_roles" in low and "rolname" in low and "join" not in low and "from" in low and "where" not in low and "*" not in low and "as" in low:
            return ("rows", [("Role name", 25), ("Attributes", 25)], [[self.svc["user"], ""]], "SELECT 1")
        if re.match(r"^(begin|start transaction)", low):
            return ("cmd", "BEGIN")
        if low.startswith(("commit", "end")):
            return ("cmd", "COMMIT")
        if low.startswith("rollback"):
            return ("cmd", "ROLLBACK")
        if low.startswith(("set ", "reset ", "discard ", "deallocate ")):
            return ("cmd", "SET")
        if low.startswith("show "):
            key = low[5:].strip()
            vals = {"server_version": "16.4 (Debian 16.4-1.pgdg120+2)", "search_path": '"$user", public', "transaction isolation level": "read committed"}
            if key in vals:
                return ("rows", [(key, 25)], [[vals[key]]], "SHOW")
            raise SqlError("42704", f'unrecognized configuration parameter "{key}"')
        if low.startswith("copy ") or "pg_read_file" in low or "pg_ls_dir" in low or "lo_import" in low or "pg_read_binary_file" in low:
            if low.startswith("copy ") and "program" in low:
                raise SqlError("42501", "permission denied to COPY to or from an external program")
            if low.startswith("copy "):
                raise SqlError("42501", "permission denied to COPY to or from a file")
            raise SqlError("42501", "permission denied for function " + ("pg_read_file" if "pg_read_file" in low else "pg_ls_dir" if "pg_ls_dir" in low else "lo_import"))
        if re.search(r"\b(pg_shadow|pg_authid)\b", low):
            raise SqlError("42501", "permission denied for table " + ("pg_authid" if "pg_authid" in low else "pg_shadow"))
        if low.startswith("select"):
            return self.select(s, low)
        if low.startswith("insert"):
            return self.insert(s)
        if low.startswith("update"):
            return self.update(s)
        if low.startswith("delete"):
            return self.delete(s)
        m = re.match(r"^drop table\s+(?:if exists\s+)?([a-z_][a-z0-9_.]*)", low)
        if m:
            name = m.group(1).replace("public.", "")
            if name not in self.store.tables:
                if "if exists" in low:
                    return ("cmd", "DROP TABLE")
                raise SqlError("42P01", f'table "{name}" does not exist')
            del self.store.tables[name]
            return ("cmd", "DROP TABLE")
        m = re.match(r"^truncate\s+(?:table\s+)?([a-z_][a-z0-9_.]*)", low)
        if m:
            self.store.table(m.group(1).replace("public.", ""))["rows"].clear()
            return ("cmd", "TRUNCATE TABLE")
        m = re.match(r"^create table\s+(?:if not exists\s+)?([a-z_][a-z0-9_]*)\s*\((.*)\)\s*$", s, re.I | re.S)
        if m:
            name, body = m.group(1).lower(), m.group(2)
            if name in self.store.tables:
                raise SqlError("42P07", f'relation "{name}" already exists')
            cols = []
            for part in split_top(body):
                tok = part.split()
                if tok and tok[0].lower() not in ("primary", "foreign", "unique", "constraint", "check"):
                    ctype = "id" if len(cols) == 0 and re.search(r"serial|primary key", part, re.I) else "text"
                    cols.append({"name": tok[0].lower(), "type": ctype})
            if not cols:
                raise SqlError("42601", "syntax error at end of input")
            if cols[0]["type"] != "id":
                cols.insert(0, {"name": "id", "type": "id"}) if all(c["name"] != "id" for c in cols) else None
            self.store.tables[name] = {"columns": cols, "rows": []}
            return ("cmd", "CREATE TABLE")
        first = s.split()[0]
        raise SqlError("42601", f'syntax error at or near "{first[:30]}"')

    # -- SELECT -----------------------------------------------------------------------------------
    def select(self, s, low):
        m = re.match(r"^select\s+(?P<cols>.+?)\s+from\s+(?P<table>[a-z_][a-z0-9_.]*)(?:\s+(?:as\s+)?[a-z_]\w*)?"
                     r"(?:\s+where\s+(?P<where>.+?))?(?:\s+order\s+by\s+(?P<order>.+?))?(?:\s+limit\s+(?P<limit>\d+))?"
                     r"(?:\s+offset\s+(?P<offset>\d+))?\s*$", s, re.I | re.S)
        if not m:
            return self.constant_select(s, low)
        name = m.group("table").lower().replace("public.", "")
        virt = self.virtual(name)
        if name.startswith(("pg_", "information_schema.")) and virt is None:
            raise SqlError("42501", f"permission denied for table {name.split('.')[-1]}")
        table = virt if virt is not None else self.store.table(name)
        store = self.store
        if virt is not None:
            store = Store({})
            store.tables = {name: virt}
        rows = store.match(name, build_preds(store, name, m.group("where"))) if virt is None else \
            [r for r in virt["rows"] if all(p(r) for p in build_preds(store, name, m.group("where")))]
        if m.group("order"):
            for item in reversed(split_top(m.group("order"))):
                bits = item.split()
                col = bits[0].split(".")[-1]
                store.column(name, col)
                rows = sorted(rows, key=lambda r, c=col: (r.get(c) is None, r.get(c)), reverse=len(bits) > 1 and bits[1].lower() == "desc")
        if m.group("offset"):
            rows = rows[int(m.group("offset")):]
        if m.group("limit") is not None:
            rows = rows[: int(m.group("limit"))]
        sel = split_top(m.group("cols"))
        if len(sel) == 1 and re.fullmatch(r"count\s*\(\s*(\*|\w+)\s*\)", sel[0], re.I):
            return ("rows", [("count", 20)], [[len(rows)]], "SELECT 1")
        if sel == ["*"]:
            cols = [c["name"] for c in table["columns"]]
        else:
            cols = []
            for item in sel:
                item = re.sub(r"\s+as\s+\w+$", "", item, flags=re.I).split(".")[-1].strip()
                store.column(name, item)
                cols.append(item)
        types = {c["name"]: c["type"] for c in table["columns"]}
        desc = [(c, sql_type_oid(types[c])) for c in cols]
        out = []
        for r in rows:
            out.append([(("t" if r.get(c) else "f") if types[c] == "bool" and r.get(c) is not None else r.get(c)) for c in cols])
        return ("rows", desc, out, f"SELECT {len(out)}")

    def constant_select(self, s, low):
        body = re.sub(r"^select\s+", "", s, flags=re.I).strip()
        consts = {"version()": PG_VERSION, "current_user": self.svc["user"], "session_user": self.svc["user"], "user": self.svc["user"],
                  "current_database()": self.svc["database"], "current_schema()": "public", "current_schema": "public",
                  "now()": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f+00"), "current_timestamp": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f+00"),
                  "inet_server_port()": "5432", "pg_backend_pid()": "412"}
        cols, vals = [], []
        for item in split_top(body):
            alias = None
            m = re.match(r"^(.*?)\s+as\s+(\w+)$", item, re.I | re.S)
            if m:
                item, alias = m.group(1).strip(), m.group(2)
            key = item.lower()
            if key in consts:
                cols.append((alias or key.replace("()", ""), 25))
                vals.append(consts[key])
            else:
                try:
                    v = literal(item)
                except SqlError:
                    raise SqlError("42703", f'column "{item[:30]}" does not exist') from None
                cols.append((alias or "?column?", 23 if isinstance(v, int) and not isinstance(v, bool) else 25))
                vals.append(v)
        return ("rows", cols, [vals], "SELECT 1")

    # -- DML ----------------------------------------------------------------------------------------
    def insert(self, s):
        m = re.match(r"^insert\s+into\s+([a-z_][a-z0-9_.]*)\s*\((.*?)\)\s*values\s*(.*?)(?:\s+returning\s+(.+))?\s*$", s, re.I | re.S)
        if not m:
            raise SqlError("42601", 'syntax error at or near "INSERT"')
        name = m.group(1).lower().replace("public.", "")
        cols = [c.strip().lower() for c in split_top(m.group(2))]
        groups = split_top(m.group(3))
        n = 0
        last = None
        for g in groups:
            g = g.strip()
            if not (g.startswith("(") and g.endswith(")")):
                raise SqlError("42601", f'syntax error at or near "{g[:20]}"')
            vals = [literal(v) for v in split_top(g[1:-1])]
            if len(vals) != len(cols):
                raise SqlError("42601", "INSERT has more expressions than target columns" if len(vals) > len(cols) else "INSERT has more target columns than expressions")
            last = self.store.insert(name, dict(zip(cols, vals, strict=True)))
            n += 1
        if m.group(4) and last is not None:
            rc = [c.strip() for c in split_top(m.group(4))]
            rc = [c["name"] for c in self.store.table(name)["columns"]] if rc == ["*"] else rc
            return ("rows", [(c, 25) for c in rc], [[last.get(c) for c in rc]], f"INSERT 0 {n}")
        return ("cmd", f"INSERT 0 {n}")

    def update(self, s):
        m = re.match(r"^update\s+([a-z_][a-z0-9_.]*)\s+set\s+(.+?)(?:\s+where\s+(.+))?\s*$", s, re.I | re.S)
        if not m:
            raise SqlError("42601", 'syntax error at or near "UPDATE"')
        name = m.group(1).lower().replace("public.", "")
        t = self.store.table(name)
        sets = {}
        for a in split_top(m.group(2)):
            col, _, val = a.partition("=")
            col = col.strip().lower()
            sets[col] = coerce(self.store.column(t, col)["type"], literal(val))
        rows = self.store.match(name, build_preds(self.store, name, m.group(3)))
        for r in rows:
            r.update(sets)
        return ("cmd", f"UPDATE {len(rows)}")

    def delete(self, s):
        m = re.match(r"^delete\s+from\s+([a-z_][a-z0-9_.]*)(?:\s+where\s+(.+))?\s*$", s, re.I | re.S)
        if not m:
            raise SqlError("42601", 'syntax error at or near "DELETE"')
        name = m.group(1).lower().replace("public.", "")
        rows = self.store.match(name, build_preds(self.store, name, m.group(2)))
        t = self.store.table(name)
        for r in rows:
            t["rows"].remove(r)
        return ("cmd", f"DELETE {len(rows)}")


async def serve_postgres(reader, writer, svc, store, log):
    peer = writer.get_extra_info("peername") or ("?", 0)
    user = None
    try:
        # ---- startup (answer SSL/GSS probes with 'N' like a server without TLS) ----
        for _ in range(4):
            length = struct.unpack("!I", await asyncio.wait_for(reader.readexactly(4), 15))[0]
            if length < 8 or length > 10_000:
                return
            payload = await asyncio.wait_for(reader.readexactly(length - 4), 15)
            code = struct.unpack("!I", payload[:4])[0]
            if code in (80877103, 80877104):
                writer.write(b"N")
                await writer.drain()
                continue
            if code != 196608:
                return
            break
        else:
            return
        kv = payload[4:].split(b"\0")
        params = {kv[i].decode("utf-8", "replace"): kv[i + 1].decode("utf-8", "replace") for i in range(0, len(kv) - 1, 2) if kv[i]}
        user = params.get("user", "")
        database = params.get("database", user)

        salt = os.urandom(4)
        writer.write(pg_msg(b"R", struct.pack("!I", 5) + salt))
        await writer.drain()
        mtype = await asyncio.wait_for(reader.readexactly(1), 30)
        mlen = struct.unpack("!I", await reader.readexactly(4))[0]
        body = await reader.readexactly(mlen - 4) if 4 < mlen < 1000 else b""
        given = body.rstrip(b"\0").decode("latin-1")
        inner = hashlib.md5((svc["password"] + svc["user"]).encode()).hexdigest()
        expected = "md5" + hashlib.md5(inner.encode() + salt).hexdigest()
        ok = mtype == b"p" and user == svc["user"] and given == expected
        log.emit(service=svc["name"], proto="postgres", action="auth", user=user[:40], database=database[:40],
                 status="ok" if ok else "failed", remote=peer[0], app=params.get("application_name", ""))
        if not ok:
            writer.write(pg_error("28P01", f'password authentication failed for user "{user}"', fatal=True))
            await writer.drain()
            return
        if database != svc["database"]:
            writer.write(pg_error("3D000", f'database "{database}" does not exist', fatal=True))
            await writer.drain()
            return
        out = pg_msg(b"R", struct.pack("!I", 0))
        for k, v in (("server_version", "16.4 (Debian 16.4-1.pgdg120+2)"), ("server_encoding", "UTF8"), ("client_encoding", "UTF8"),
                     ("DateStyle", "ISO, MDY"), ("integer_datetimes", "on"), ("standard_conforming_strings", "on"),
                     ("TimeZone", "Etc/UTC"), ("is_superuser", "off"), ("session_authorization", user), ("application_name", params.get("application_name", ""))):
            out += pg_msg(b"S", k.encode() + b"\0" + v.encode() + b"\0")
        out += pg_msg(b"K", struct.pack("!II", 412, int.from_bytes(os.urandom(3), "big"))) + pg_msg(b"Z", b"I")
        writer.write(out)
        await writer.drain()

        engine, skipping = PgEngine(store, svc), False
        while True:
            t = await asyncio.wait_for(reader.readexactly(1), IDLE_TIMEOUT)
            n = struct.unpack("!I", await reader.readexactly(4))[0]
            if n < 4 or n > MAX_BODY:
                return
            data = await reader.readexactly(n - 4)
            if t == b"X":
                return
            if t == b"Q":
                sql = data.rstrip(b"\0").decode("utf-8", "replace")
                reply = b""
                statements = split_statements(sql) or [""]
                for stmt in statements:
                    unsupported = False
                    try:
                        res = engine.run(stmt)
                    except SqlError as e:
                        unsupported = e.code == "42601"
                        reply += pg_error(e.code, e.message)
                        log.emit(service=svc["name"], proto="postgres", action="sql", sql=stmt, status="error", code=e.code,
                                 user=user, remote=peer[0], unsupported=unsupported)
                        break  # a failing statement aborts the rest of the simple-query string
                    log.emit(service=svc["name"], proto="postgres", action="sql", sql=stmt, status="ok", user=user, remote=peer[0])
                    if res[0] == "empty":
                        reply += pg_msg(b"I")
                    elif res[0] == "cmd":
                        reply += pg_msg(b"C", res[1].encode() + b"\0")
                    else:
                        _, desc, rows, tag = res
                        reply += pg_rowdesc(desc) + b"".join(pg_datarow(r) for r in rows) + pg_msg(b"C", tag.encode() + b"\0")
                writer.write(reply + pg_msg(b"Z", b"I"))
                await writer.drain()
            elif t in (b"P", b"B", b"D", b"E", b"C", b"H"):  # extended protocol: not supported, fail until Sync
                if not skipping:
                    writer.write(pg_error("0A000", "extended query protocol is not supported"))
                    skipping = True
                    log.emit(service=svc["name"], proto="postgres", action="extended-protocol", status="error", user=user, remote=peer[0], unsupported=True)
                    await writer.drain()
            elif t == b"S":
                skipping = False
                writer.write(pg_msg(b"Z", b"I"))
                await writer.drain()
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, struct.error):
        pass
    finally:
        writer.close()


# ---- wiring -------------------------------------------------------------------------------------------------
async def main(config_path):
    cfg = json.load(open(config_path, encoding="utf-8"))
    log = EventLog(os.environ.get("HORNET_EVENTS", cfg.get("events", "/run/hornet/events.jsonl")))
    stores = {sid: Store(s["tables"]) for sid, s in cfg.get("stores", {}).items()}
    servers, started = [], []
    for svc in cfg["services"]:
        store = stores.get(svc.get("store")) or Store({})
        redis_state = RedisState(svc.get("keys"))
        handler = {
            "http": lambda r, w, svc=svc, store=store: serve_http(r, w, svc, store, log),
            "postgres": lambda r, w, svc=svc, store=store: serve_postgres(r, w, svc, store, log),
            "redis": lambda r, w, svc=svc, st=redis_state: serve_redis(r, w, svc, st, log),
        }.get(svc["kind"])
        if handler is None:
            continue
        try:
            servers.append(await asyncio.start_server(handler, svc.get("bind", "127.0.0.1"), svc["port"], limit=2 ** 16))
            started.append(svc["name"])
        except OSError as exc:
            log.emit(type="emulator_error", service=svc["name"], error=type(exc).__name__)
    log.emit(type="ready", services=started)
    await asyncio.gather(*(s.serve_forever() for s in servers)) if servers else await asyncio.sleep(10 ** 9)


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "/usr/share/hornet/services.json"))
    except KeyboardInterrupt:
        pass
