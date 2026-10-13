"""Sanitized profile -> environment plan. Deterministic for a given (profile, clock)."""
import re
from datetime import datetime

from app.ai.enrichment import EnrichmentReport, enrich
from app.ai.provider import AIProvider
from app.environment.data_model import pick_model
from app.environment.plan import EnvPlan, ServicePlan, UserSpec
from app.environment.roles import DB_ROLES, ROLE_BUILDERS, ROLE_ORDER, Ctx, clock
from app.environment.service_fixtures import add_services_file
from app.environment.synthetic_data import Synth
from app.profile import SanitizedProfile

# Debian base accounts that exist in every twin (uid 1000/1001 are the attacker and PID 1 accounts).
BASE_USERS = [
    UserSpec("root", 0, 0, "/root", "/bin/bash", "root", "system"),
    UserSpec("daemon", 1, 1, "/usr/sbin", "/usr/sbin/nologin", "daemon", "system"),
    UserSpec("bin", 2, 2, "/bin", "/usr/sbin/nologin", "bin", "system"),
    UserSpec("sys", 3, 3, "/dev", "/usr/sbin/nologin", "sys", "system"),
    UserSpec("sync", 4, 65534, "/bin", "/bin/sync", "sync", "system"),
    UserSpec("games", 5, 60, "/usr/games", "/usr/sbin/nologin", "games", "system"),
    UserSpec("man", 6, 12, "/var/cache/man", "/usr/sbin/nologin", "man", "system"),
    UserSpec("lp", 7, 7, "/var/spool/lpd", "/usr/sbin/nologin", "lp", "system"),
    UserSpec("mail", 8, 8, "/var/mail", "/usr/sbin/nologin", "mail", "system"),
    UserSpec("news", 9, 9, "/var/spool/news", "/usr/sbin/nologin", "news", "system"),
    UserSpec("proxy", 13, 13, "/bin", "/usr/sbin/nologin", "proxy", "system"),
    UserSpec("www-data", 33, 33, "/var/www", "/usr/sbin/nologin", "www-data", "system"),
    UserSpec("backup", 34, 34, "/var/backups", "/usr/sbin/nologin", "backup", "system"),
    UserSpec("nobody", 65534, 65534, "/nonexistent", "/usr/sbin/nologin", "nobody", "system"),
    UserSpec("user", 1000, 1000, "/home/user", "/bin/bash", "", "human"),
    UserSpec("hornet-sys", 1001, 1001, "/nonexistent", "/usr/sbin/nologin", "", "system"),
]


def db_name_for(project: str) -> str:
    base = re.sub(r"[^a-z0-9_]", "", project.split("-")[0].lower())
    return base if base and base[0].isalpha() else "app"


def _passwd(plan: EnvPlan) -> str:
    return "".join(
        f"{u.name}:x:{u.uid}:{u.gid}:{u.comment}:{u.home}:{u.shell}\n"
        for u in sorted(plan.users.values(), key=lambda u: (u.uid, u.name))
    )


def _group(plan: EnvPlan) -> str:
    return "".join(f"{u.name}:x:{u.gid}:\n" for u in sorted(plan.users.values(), key=lambda u: (u.gid, u.name)))


def build_plan(
    profile: SanitizedProfile,
    now: datetime,
    provider: AIProvider | None = None,
    forbidden: tuple[str, ...] = (),
) -> tuple[EnvPlan, EnrichmentReport | None]:
    now = clock(now)
    seed = int(profile.profile_hash[:16], 16)
    synth = Synth(seed, now)
    plan = EnvPlan(hostname=f"{profile.source}-01", project=profile.source)
    plan.people = synth.people(8)
    for u in BASE_USERS:
        plan.add_user(UserSpec(**u.__dict__))
    # a data model is only needed when something in the environment stores or serves data
    needs_data = any(s.role in DB_ROLES or s.role == "http-app" for s in profile.services)
    model, model_info = pick_model(profile, provider) if needs_data else (None, {"source": "none"})
    ctx = Ctx(plan=plan, synth=synth, profile=profile, db_name=db_name_for(profile.source), model=model)

    ordered = sorted(profile.services, key=lambda s: (ROLE_ORDER.index(s.role) if s.role in ROLE_ORDER else 99, s.name))
    for i, spec in enumerate(ordered):
        ports = [(p.container_port, p.protocol) for p in spec.ports]
        sp = ServicePlan(spec.name, spec.role, spec.image, synth.internal_ip(i), ports)
        plan.services.append(sp)
        ctx.sps[spec.name] = sp
    for spec in ordered:
        ROLE_BUILDERS.get(spec.role, ROLE_BUILDERS["generic"])(ctx, spec, ctx.sps[spec.name])

    add_services_file(ctx)
    plan.add_file("/etc/motd", f"{profile.source} production host. Authorized use only.\n")
    plan.add_file("/etc/passwd", _passwd(plan))
    plan.add_file("/etc/group", _group(plan))

    report = enrich(plan, profile, provider, ctx.db_name, forbidden) if provider else None
    plan.ensure_parents()
    plan.notes = {"db_name": ctx.db_name, "built_at": now.isoformat(), "data_model": model_info}
    return plan, report
