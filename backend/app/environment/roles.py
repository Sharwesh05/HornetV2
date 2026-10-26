"""Role builders: turn one service of the sanitized profile into synthetic files, users, data, logs.

Nothing here knows what the observed server does. Everything comes from the profile (images, ports,
mounts, workdir, user, env var names) plus the DataModel chosen in data_model.py. To support a new kind
of server, add a function and register it in ROLE_BUILDERS; anything unrecognised uses build_generic,
so any Docker environment gets a twin.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from app.environment import data_model as dm
from app.environment.plan import DatabasePlan, EnvPlan, ServicePlan, UserSpec
from app.environment.synthetic_data import Synth
from app.profile import SanitizedProfile, ServiceSpec

DB_ROLES = ("postgres", "mysql")
ENGINE_PORT = {"postgres": 5432, "mysql": 3306, "redis": 6379}
_SAFE_DIR = re.compile(r"^/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")


@dataclass
class Ctx:
    plan: EnvPlan
    synth: Synth
    profile: SanitizedProfile
    db_name: str
    model: dm.DataModel | None = None
    sps: dict[str, ServicePlan] = field(default_factory=dict)
    _rows: dict[str, list[dict]] | None = None

    def sp_by_role(self, *roles: str) -> ServicePlan | None:
        return next((sp for sp in self.sps.values() if sp.role in roles), None)

    def table_data(self) -> dict[str, list[dict]]:
        if self.model is None:
            return {}
        if self._rows is None:
            self._rows = dm.generate_rows(self.model, self.synth, self.plan.people)
        return self._rows

    def stamp(self, minutes_ago: int, fmt: str) -> str:
        return self.synth.ts(minutes_ago).strftime(fmt)


# ---- helpers ------------------------------------------------------------------------------------
def ensure_user(ctx: Ctx, name: str, home: str, shell: str = "/usr/sbin/nologin", uid: int | None = None,
                comment: str = "") -> UserSpec:
    existing = ctx.plan.users.get(name)
    if existing:
        return existing
    uid = uid if uid is not None else ctx.plan.next_uid()
    return ctx.plan.add_user(UserSpec(name, uid, uid, home, shell, comment, "service"))


def safe_dir(path: str | None, fallback: str, under: str = "/") -> str:
    """Only plain absolute paths from the profile are used as real directories in the image."""
    if path and _SAFE_DIR.match(path) and ".." not in path.split("/") and path.startswith(under) and path != "/":
        return path.rstrip("/")
    return fallback


def dedupe(ctx: Ctx, path: str, sp: ServicePlan) -> str:
    """Two services of the same kind (two Redis, two Postgres) must not share config/log/init files."""
    if path not in ctx.plan.files:
        return path
    base, dot, ext = path.rpartition(".")
    tag = re.sub(r"[^a-z0-9]", "-", sp.name.lower())
    return f"{base}-{tag}.{ext}" if dot and "/" not in ext else f"{path}-{tag}"


def db_label(ctx: Ctx, sp: ServicePlan) -> str:
    """The first database service owns the primary name; others get a distinct one."""
    first = ctx.sp_by_role(*DB_ROLES)
    return ctx.db_name if first is None or first.name == sp.name else f"{ctx.db_name}_{re.sub('[^a-z0-9]', '_', sp.name.lower())}"


def _access_log(ctx: Ctx, app_paths: list[str], n: int = 40) -> str:
    s, lines = ctx.synth, []
    agents = ["curl/8.5.0", "Mozilla/5.0 (X11; Linux x86_64)", "python-requests/2.31.0", "Go-http-client/1.1"]
    for i in range(n):
        path = s.rng.choice(app_paths)
        status = s.rng.choices([200, 304, 404, 403], weights=[80, 8, 6, 6])[0]
        lines.append(
            f'{s.public_ip()} - - [{ctx.stamp((n - i) * 3, "%d/%b/%Y:%H:%M:%S +0000")}] "GET {path} HTTP/1.1" '
            f'{status} {s.rng.randint(120, 4096)} "-" "{s.rng.choice(agents)}"'
        )
    return "\n".join(lines) + "\n"


def _write_conf_and_log(ctx: Ctx, sp: ServicePlan, conf_path: str, conf: str, log_path: str, log: str,
                        uid: int = 0, gid: int = 0) -> tuple[str, str]:
    """Config files are root-owned and world-readable; logs belong to the service account."""
    conf_path, log_path = dedupe(ctx, conf_path, sp), dedupe(ctx, log_path, sp)
    ctx.plan.add_file(conf_path, conf, 0o644, 0, 0)
    ctx.plan.add_file(log_path, log, 0o644, uid, gid)
    sp.config_paths.append(conf_path)
    sp.log_paths.append(log_path)
    return conf_path, log_path


def api_paths(ctx: Ctx) -> list[str]:
    return ["/health"] + [f"/api/{t}" for t in ctx.table_data()]


# ---- env var values for generated .env files ---------------------------------------------------
def _db_pw(ctx: Ctx) -> tuple[ServicePlan | None, str | None]:
    db = ctx.sp_by_role(*DB_ROLES)
    return (db, ctx.plan.secret(f"{db.name}.password", ctx.synth.password)) if db else (None, None)


def env_value(ctx: Ctx, name: str, cls: str, sp: ServicePlan, env_path: str) -> str:
    n = name.upper()
    plan, s = ctx.plan, ctx.synth
    redis = ctx.sp_by_role("redis")
    db, db_pw = _db_pw(ctx)

    if cls == "connection":
        if ("REDIS" in n or "CACHE" in n or "BROKER" in n) and redis:
            pw = plan.secret(f"{redis.name}.password", s.password)
            plan.expect(env_path, f"{redis.name}.password")
            return f"redis://:{pw}@{redis.name}:6379/0"
        if db and db_pw:
            scheme = "postgresql" if db.role == "postgres" else "mysql"
            plan.expect(env_path, f"{db.name}.password")
            return f"{scheme}://{ctx.db_name}:{db_pw}@{db.name}:{ENGINE_PORT[db.role]}/{ctx.db_name}"
        return "http://localhost:8080"

    if cls == "secret":
        if "STRIPE" in n:
            return plan.secret(f"{sp.name}.{name}", s.stripe_key)
        if "AWS" in n and "ID" in n:
            return plan.secret(f"{sp.name}.{name}", s.aws_key_id)
        if re.search(r"PASS(WORD|WD)?|PWD", n):
            if "REDIS" in n and redis:
                plan.expect(env_path, f"{redis.name}.password")
                return plan.secret(f"{redis.name}.password", s.password)
            if db and re.search(r"POSTGRES|MYSQL|MARIADB|DB|DATABASE", n):
                plan.expect(env_path, f"{db.name}.password")
                return db_pw or ""
            return plan.secret(f"{sp.name}.{name}", s.password)
        return plan.secret(f"{sp.name}.{name}", s.token)

    port = str(sp.ports[0][0]) if sp.ports else "8080"
    exact = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC", "PATH": "/usr/local/bin:/usr/bin:/bin",
             "HOSTNAME": sp.name, "HOME": sp.root or "/home/app"}
    if n in exact:
        return exact[n]
    if n.endswith("PORT"):
        return port
    if "LOG_LEVEL" in n:
        return "info"
    if "LOG_FILE" in n:
        return f"{sp.log_dir or '/var/log/' + sp.name}/app.log"
    if n in ("ENV", "ENVIRONMENT") or n.endswith(("_ENV", "_ENVIRONMENT")):
        return "production"
    if "DEBUG" in n:
        return "false"
    if n.endswith("_HOST"):
        return redis.name if redis and "REDIS" in n else db.name if db else "localhost"
    if n.endswith(("_DB", "_DATABASE", "_NAME", "_USER", "_USERNAME")):
        return ctx.db_name
    return "default"


# ---- role builders -------------------------------------------------------------------------------
def build_postgres(ctx: Ctx, spec: ServiceSpec, sp: ServicePlan) -> None:
    plan, ver = ctx.plan, "16"
    pg = ensure_user(ctx, "postgres", "/var/lib/postgresql", "/bin/bash", uid=105, comment="PostgreSQL administrator")
    sp.user = "postgres"
    db = db_label(ctx, sp)
    pw = plan.secret(f"{sp.name}.password", ctx.synth.password)
    plan.databases.append(DatabasePlan(db, "postgres", sp.name, db, ctx.table_data()))

    conf = (
        f"# PostgreSQL {ver} configuration\ndata_directory = '/var/lib/postgresql/{ver}/main'\n"
        f"hba_file = '/etc/postgresql/{ver}/main/pg_hba.conf'\nlisten_addresses = '*'\nport = 5432\n"
        "max_connections = 100\nshared_buffers = 128MB\nlog_connections = on\nlog_timezone = 'UTC'\n"
    )
    log = "\n".join(
        [f"{ctx.stamp(300, '%Y-%m-%d %H:%M:%S')} UTC [1] LOG:  database system is ready to accept connections"]
        + [f"{ctx.stamp(200 - i * 20, '%Y-%m-%d %H:%M:%S')} UTC [{40 + i}] LOG:  connection authorized: user={db} database={db}"
           for i in range(6)]
    ) + "\n"
    _write_conf_and_log(ctx, sp, f"/etc/postgresql/{ver}/main/postgresql.conf", conf,
                        f"/var/log/postgresql/postgresql-{ver}-main.log", log, pg.uid, pg.gid)
    plan.add_file(f"/etc/postgresql/{ver}/main/pg_hba.conf", "local all postgres peer\nhost all all 0.0.0.0/0 scram-sha-256\n")
    plan.add_file(f"/var/lib/postgresql/{ver}/main/PG_VERSION", f"{ver}\n", 0o600, pg.uid, pg.gid)

    init = (f"CREATE ROLE {db} LOGIN PASSWORD '{pw}';\nCREATE DATABASE {db} OWNER {db};\n"
            + (dm.schema_sql(ctx.model, "postgres") + dm.inserts_sql(ctx.model, ctx.table_data(), "postgres") + "\n" if ctx.model else ""))
    init_path = dedupe(ctx, "/docker-entrypoint-initdb.d/01-schema.sql", sp)
    plan.add_file(init_path, init, 0o644, sensitivity="synthetic-sensitive")
    plan.expect(init_path, f"{sp.name}.password")

    pgpass = "/home/user/.pgpass"  # bait an attacker will find in their own home
    line = f"{sp.name}:5432:{db}:{db}:{pw}\n"
    prior = (plan.files[pgpass].content or "") if pgpass in plan.files else ""
    plan.add_file(pgpass, prior + line, 0o600, 1000, 1000, sensitivity="synthetic-sensitive")
    plan.expect(pgpass, f"{sp.name}.password")


def build_mysql(ctx: Ctx, spec: ServiceSpec, sp: ServicePlan) -> None:
    plan = ctx.plan
    my = ensure_user(ctx, "mysql", "/nonexistent", uid=107, comment="MySQL Server")
    sp.user = "mysql"
    db = db_label(ctx, sp)
    pw = plan.secret(f"{sp.name}.password", ctx.synth.password)
    plan.databases.append(DatabasePlan(db, "mysql", sp.name, db, ctx.table_data()))
    conf = "[mysqld]\nbind-address = 0.0.0.0\nport = 3306\ndatadir = /var/lib/mysql\nmax_connections = 151\n"
    log = f"{ctx.stamp(300, '%Y-%m-%dT%H:%M:%S')}Z 0 [System] [MY-010931] [Server] ready for connections. Port: 3306\n"
    _write_conf_and_log(ctx, sp, "/etc/mysql/my.cnf", conf, "/var/log/mysql/error.log", log, my.uid, my.gid)
    body = (dm.schema_sql(ctx.model, "mysql") + dm.inserts_sql(ctx.model, ctx.table_data(), "mysql") + "\n") if ctx.model else ""
    init_path = dedupe(ctx, "/docker-entrypoint-initdb.d/01-schema.sql", sp)
    plan.add_file(init_path, f"CREATE USER '{db}'@'%' IDENTIFIED BY '{pw}';\n{body}", sensitivity="synthetic-sensitive")
    plan.expect(init_path, f"{sp.name}.password")


def build_redis(ctx: Ctx, spec: ServiceSpec, sp: ServicePlan) -> None:
    rd = ensure_user(ctx, "redis", "/var/lib/redis", uid=106, comment="Redis server")
    sp.user = "redis"
    pw = ctx.plan.secret(f"{sp.name}.password", ctx.synth.password)
    conf = f"bind 0.0.0.0\nport 6379\nrequirepass {pw}\nmaxmemory 256mb\nappendonly no\n"
    log = (f"1:M {ctx.stamp(300, '%d %b %Y %H:%M:%S')}.123 * Ready to accept connections tcp\n"
           f"1:M {ctx.stamp(120, '%d %b %Y %H:%M:%S')}.456 * 1 changes in 3600 seconds. Saving...\n")
    conf_path, _ = _write_conf_and_log(ctx, sp, "/etc/redis/redis.conf", conf, "/var/log/redis/redis-server.log", log, rd.uid, rd.gid)
    ctx.plan.expect(conf_path, f"{sp.name}.password")


def build_nginx(ctx: Ctx, spec: ServiceSpec, sp: ServicePlan) -> None:
    upstream = ctx.sp_by_role("http-app")
    target = f"{upstream.name}:{upstream.ports[0][0]}" if upstream and upstream.ports else "127.0.0.1:8080"
    sp.user = "www-data"
    conf = (
        "worker_processes auto;\nevents { worker_connections 1024; }\nhttp {\n"
        f"  upstream backend {{ server {target}; }}\n  server {{\n    listen {sp.ports[0][0] if sp.ports else 80};\n"
        "    server_name _;\n    location / { proxy_pass http://backend; }\n  }\n}\n"
    )
    _, log_path = _write_conf_and_log(ctx, sp, "/etc/nginx/nginx.conf", conf, "/var/log/nginx/access.log",
                                      _access_log(ctx, ["/"] + api_paths(ctx) + ["/favicon.ico"]))
    ctx.plan.add_file(dedupe(ctx, "/var/log/nginx/error.log", sp), f"{ctx.stamp(500, '%Y/%m/%d %H:%M:%S')} [notice] 1#1: nginx started\n", 0o644)


def runtime_hint(spec: ServiceSpec) -> str:
    hay = " ".join([spec.image, *spec.entrypoint, *spec.command]).lower()
    if re.search(r"python|gunicorn|uvicorn|flask|django|fastapi|celery", hay):
        return "python"
    if re.search(r"\bnode\b|npm|yarn|pnpm|express|next", hay):
        return "node"
    return "unknown"


def _app_stub(ctx: Ctx, spec: ServiceSpec, sp: ServicePlan, root: str, uid: int, gid: int) -> None:
    """A small, language-appropriate source stub, chosen from hints in the profile. No framework is assumed."""
    plan, hint = ctx.plan, runtime_hint(spec)
    port = sp.ports[0][0] if sp.ports else 8080
    db, redis = ctx.sp_by_role(*DB_ROLES), ctx.sp_by_role("redis")
    tables = list(ctx.table_data())
    if hint == "python":
        routes = ["/health"] + [f"/api/{t}" for t in tables]
        code = (
            f'"""{sp.name} service."""\nimport json\nimport os\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\n\n'
            f"ROUTES = {routes!r}\n\n\nclass Handler(BaseHTTPRequestHandler):\n    def do_GET(self):\n"
            "        if self.path not in ROUTES:\n            self.send_error(404)\n            return\n"
            "        body = json.dumps({'status': 'ok'} if self.path == '/health' else []).encode()\n"
            "        self.send_response(200)\n        self.send_header('Content-Type', 'application/json')\n"
            "        self.end_headers()\n        self.wfile.write(body)\n\n\n"
            f"if __name__ == '__main__':\n    HTTPServer(('0.0.0.0', int(os.environ.get('PORT', {port}))), Handler).serve_forever()\n"
        )
        plan.add_file(f"{root}/app/main.py", code, 0o644, uid, gid)
        plan.add_file(f"{root}/app/__init__.py", "", 0o644, uid, gid)
        reqs = ["gunicorn"] + (["psycopg[binary]"] if db and db.role == "postgres" else ["PyMySQL"] if db else []) + (["redis"] if redis else [])
        plan.add_file(f"{root}/requirements.txt", "\n".join(reqs) + "\n", 0o644, uid, gid)
    elif hint == "node":
        deps = {**({"pg": "^8.11.0"} if db and db.role == "postgres" else {"mysql2": "^3.9.0"} if db else {}),
                **({"redis": "^4.6.0"} if redis else {})}
        pkg = json.dumps({"name": sp.name, "version": "1.0.0", "scripts": {"start": "node src/index.js"},
                          "dependencies": deps}, indent=2) + "\n"
        plan.add_file(f"{root}/package.json", pkg, 0o644, uid, gid)
        plan.add_file(f"{root}/src/index.js",
                      f"const http = require('http');\nhttp.createServer((req, res) => res.end('ok')).listen(process.env.PORT || {port});\n",
                      0o644, uid, gid)
    else:
        cfg = (f"server:\n  port: {port}\ndatasource:\n  host: {db.name if db else 'none'}\n  name: {ctx.db_name}\n"
               f"cache:\n  host: {redis.name if redis else 'none'}\n")
        plan.add_file(f"{root}/config/application.yml", cfg, 0o644, uid, gid)


def build_http_app(ctx: Ctx, spec: ServiceSpec, sp: ServicePlan) -> None:
    plan, name = ctx.plan, sp.name
    root = safe_dir(spec.workdir, f"/var/www/{name}")
    uname = spec.user if spec.user and re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", spec.user) else name
    user = ensure_user(ctx, uname, root, "/usr/sbin/nologin", comment=f"{name} service account")
    sp.user, sp.root = user.name, root
    uid, gid = user.uid, user.gid
    # the real service writes its logs to a mounted directory under /var/log: use the same place
    sp.log_dir = next((d for d in (safe_dir(m.destination, "", "/var/log/") for m in spec.mounts) if d), f"/var/log/{name}")
    log_dir = sp.log_dir

    endpoints = api_paths(ctx)
    env_path = f"{root}/.env"
    lines = [f"{n}={env_value(ctx, n, spec.env_classes.get(n, 'config'), sp, env_path)}" for n in spec.env_names]
    plan.add_file(env_path, "\n".join(lines) + ("\n" if lines else ""), 0o600, uid, gid, sensitivity="synthetic-sensitive")
    _app_stub(ctx, spec, sp, root, uid, gid)
    plan.add_file(f"{root}/README.md", f"# {name}\n\nInternal service. Config lives in `.env`. Logs: {log_dir}/\n", 0o644, uid, gid)

    app_log = "".join(
        f"{ctx.stamp((30 - i) * 4, '%Y-%m-%d %H:%M:%S')},{ctx.synth.rng.randint(0, 999):03d} INFO {name} "
        f"request_id={ctx.synth.request_id()} path={ctx.synth.rng.choice(endpoints)} status=200\n"
        for i in range(30)
    )
    guarded = endpoints[-1]
    denied = f"{ctx.stamp(15, '%Y-%m-%d %H:%M:%S')},120 WARNING {name} denied access to {guarded} from {ctx.synth.public_ip()}\n"
    plan.add_file(f"{log_dir}/app.log", app_log + denied, 0o644, uid, gid)
    plan.add_file(f"{log_dir}/access.log", _access_log(ctx, endpoints), 0o644, uid, gid)
    sp.config_paths.append(env_path)
    sp.log_paths += [f"{log_dir}/app.log", f"{log_dir}/access.log"]


def build_generic(ctx: Ctx, spec: ServiceSpec, sp: ServicePlan) -> None:
    name = sp.name
    user = ensure_user(ctx, re.sub(r"[^a-z0-9_-]", "", name)[:30] or "svc", f"/opt/{name}", comment=f"{name} service")
    sp.user = user.name
    port_txt = ", ".join(f"{p}/{proto}" for p, proto in sp.ports) or "none"
    conf = f"# {name}\nimage = {spec.image}\nlisten = {port_txt}\n"
    log = f"{ctx.stamp(240, '%Y-%m-%d %H:%M:%S')} INFO {name} started\n"
    _write_conf_and_log(ctx, sp, f"/etc/{name}/{name}.conf", conf, f"/var/log/{name}/{name}.log", log, user.uid, user.gid)
    ctx.plan.add_file(f"/opt/{name}/README.md", f"# {name}\n\nSee /etc/{name}/{name}.conf\n", 0o644, user.uid, user.gid)


ROLE_BUILDERS = {
    "postgres": build_postgres,
    "mysql": build_mysql,
    "redis": build_redis,
    "nginx": build_nginx,
    "http-app": build_http_app,
    "generic": build_generic,
}
# data stores first, so apps can reference their (fake) credentials; the order is also deterministic
ROLE_ORDER = ["postgres", "mysql", "redis", "http-app", "nginx", "generic"]


def clock(now: datetime) -> datetime:
    return now.replace(microsecond=0)
