"""Plan -> the config file for the in-twin service emulators (twin-image/emulator/hornet_emulator.py).

Services that are real servers in the observed environment (a database, a cache, a web app, a proxy) get
a running fake inside the twin, each on its own loopback address, reachable by its original name (the
runtime adds `name -> 127.0.1.N` host entries). All of them read the same fixture data, so a row created
through the HTTP API shows up in the SQL database. The fake credentials are the same ones written into the
files the attacker can find (.env, ~/.pgpass, redis.conf), so what they steal actually works.
"""
import json

from app.environment.roles import DB_ROLES, Ctx, runtime_hint

SERVICES_PATH = "/usr/share/hornet/services.json"
EVENTS_PATH = "/run/hornet/events.jsonl"
SYSTEM_UID = 1001
EMULATED = {"postgres": "postgres", "redis": "redis", "http-app": "http", "nginx": "http"}
DEFAULT_PORT = {"postgres": 5432, "redis": 6379, "http-app": 8080, "nginx": 80}


def _store(ctx: Ctx) -> dict:
    rows = ctx.table_data()
    return {"tables": {t.name: {"columns": [{"name": c.name, "type": c.type, "ref": c.ref} for c in t.columns],
                                "rows": rows[t.name]} for t in ctx.model.tables}}


def _redis_keys(ctx: Ctx) -> dict:
    keys = {f"cache:{t}:count": str(len(r)) for t, r in ctx.table_data().items()}
    for i, p in enumerate(ctx.plan.people[:3], 1):
        keys[f"session:{i}"] = json.dumps({"user": p.email})
    return keys


def build_services_config(ctx: Ctx) -> dict | None:
    plan = ctx.plan
    emulated = [sp for sp in plan.services if sp.role in EMULATED]
    if not emulated:
        return None
    stores: dict[str, dict] = {}
    db_sp = ctx.sp_by_role(*DB_ROLES)
    default_store = db_sp.name if db_sp else "default"
    if ctx.model is not None:
        for sp in plan.services:
            if sp.role in DB_ROLES:
                stores[sp.name] = _store(ctx)
        stores.setdefault(default_store, _store(ctx))

    hints = {s.name: runtime_hint(s) for s in ctx.profile.services}
    services = []
    for n, sp in enumerate(emulated):
        sp.loopback, sp.emulator = f"127.0.1.{10 + n}", EMULATED[sp.role]
        port = sp.ports[0][0] if sp.ports else DEFAULT_PORT[sp.role]
        entry = {"name": sp.name, "kind": sp.emulator, "bind": sp.loopback, "port": port}
        if sp.role == "postgres":
            db = next(d for d in plan.databases if d.service == sp.name)
            entry.update(store=sp.name, database=db.name, user=db.owner, password=plan.secrets[f"{sp.name}.password"])
        elif sp.role == "redis":
            entry.update(password=plan.secrets[f"{sp.name}.password"], keys=_redis_keys(ctx))
        else:  # http-app, nginx
            entry.update(store=default_store, server="gunicorn" if hints.get(sp.name) == "python" else "nginx/1.22.1")
        services.append(entry)
    return {"events": EVENTS_PATH, "stores": stores, "services": services}


def add_services_file(ctx: Ctx) -> None:
    cfg = build_services_config(ctx)
    if cfg is None:
        return
    ctx.plan.add_file(SERVICES_PATH, json.dumps(cfg, indent=1) + "\n", 0o600, SYSTEM_UID, SYSTEM_UID,
                      sensitivity="synthetic-sensitive")
    for svc in cfg["services"]:
        if "password" in svc:  # the emulator must accept exactly the fake credentials written elsewhere
            ctx.plan.expect(SERVICES_PATH, f"{svc['name']}.password")
