"""Validate a plan before anything is built or published (README section 4.2, steps 6 and 8).

Checks: graph invariants, cross-resource consistency (users, credentials, logs, services), size
limits, and a leak scan that proves nothing sensitive was copied into the generated files.
"""
import re
from dataclasses import dataclass, field

from app.environment.graph_nodes import plan_nodes
from app.environment.plan import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES, EnvPlan
from app.graph import NodeSpec, PathError, normalize_path, validate_tree
from app.sanitization.patterns import SECRET_KINDS, find_secrets

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_OK_IP = re.compile(r"^(?:203\.0\.113\.\d+|198\.51\.100\.\d+|10\.20\.0\.\d+|127\.0\.\d+\.\d+|0\.0\.0\.0)$")


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors[:50], "stats": self.stats}


def _scrub(text: str, secrets: list[str]) -> str:
    for v in secrets:
        text = text.replace(v, "<REDACTED:fake>")
    return text


def validate_plan(plan: EnvPlan, forbidden: tuple[str, ...] = ()) -> ValidationReport:
    rep, err = ValidationReport(), None
    err = rep.errors

    # --- structure and graph invariants ---------------------------------------------------
    files = plan.files
    if len(files) > MAX_FILES:
        err.append(f"too many files ({len(files)} > {MAX_FILES})")
    total = sum(len((f.content or "").encode()) for f in files.values())
    if total > MAX_TOTAL_BYTES:
        err.append("total content too large")
    for path, f in files.items():
        try:
            if normalize_path(path) != path:
                err.append(f"path not normalized: {path!r}")
        except PathError as exc:
            err.append(f"{path!r}: {exc}")
        if f.kind == "file" and len((f.content or "").encode()) > MAX_FILE_BYTES:
            err.append(f"file too large: {path}")

    nodes = plan_nodes(plan)
    specs = [NodeSpec(n.path, n.parent_path, n.path, n.node_type) for n in nodes]
    err.extend(validate_tree(specs))

    # --- users, owners, services -------------------------------------------------------------
    uids = {u.uid for u in plan.users.values()}
    gids = {u.gid for u in plan.users.values()}
    if len({u.uid for u in plan.users.values()}) != len(plan.users):
        err.append("duplicate uid in /etc/passwd")
    for path, f in files.items():
        if f.uid not in uids or f.gid not in gids:
            err.append(f"{path}: owner {f.uid}:{f.gid} is not a known user/group")
    for sp in plan.services:
        if sp.user and sp.user not in plan.users:
            err.append(f"service {sp.name}: user {sp.user!r} missing from /etc/passwd")
        if not sp.config_paths or not sp.log_paths:
            err.append(f"service {sp.name}: needs at least one config file and one log file")
        for p in sp.config_paths + sp.log_paths:
            if p not in files or files[p].kind != "file":
                err.append(f"service {sp.name}: {p} is referenced but does not exist")
    service_names = {sp.name for sp in plan.services}
    for d in plan.databases:
        if d.service not in service_names:
            err.append(f"database {d.name}: service {d.service!r} does not exist")
        if not d.tables or any(not rows for rows in d.tables.values()):
            err.append(f"database {d.name}: empty table")
    passwd = files.get("/etc/passwd")
    if passwd is None or any(f"{u.name}:x:{u.uid}:" not in (passwd.content or "") for u in plan.users.values()):
        err.append("/etc/passwd does not match the planned users")

    # --- credential consistency across resources ----------------------------------------------
    for path, secret_name in plan.expectations:
        holder, value = files.get(path), plan.secrets.get(secret_name)
        if holder is None or value is None:
            err.append(f"credential {secret_name!r} expected in {path} but one of them is missing")
        elif value not in (holder.content or ""):
            err.append(f"credential {secret_name!r} is not consistent with {path}")

    # --- generated data may only mention the synthetic world --------------------------------
    people = {p.email for p in plan.people}
    secrets = sorted(plan.secrets.values(), key=len, reverse=True)
    for path, f in files.items():
        if f.kind != "file":
            continue
        text = f.content or ""
        for e in _EMAIL.findall(text):
            if e not in people:
                err.append(f"{path}: unknown email address")
        for ip in _IP.findall(text):
            if not _OK_IP.match(ip):
                err.append(f"{path}: non-synthetic IP address")
        # --- leak scan: with the planned fake secrets removed, nothing secret-like may remain
        for finding in find_secrets(_scrub(text, secrets), SECRET_KINDS):
            err.append(f"{path}: unexpected secret-like data ({finding.kind})")
        for needle in forbidden:
            if needle and needle in text:
                err.append(f"{path}: contains a forbidden value")
    for needle in forbidden:
        if needle and any(needle in n.path for n in nodes):
            err.append("a forbidden value appears in the tree")

    rep.stats = {
        "files": sum(1 for f in files.values() if f.kind == "file"),
        "dirs": sum(1 for f in files.values() if f.kind == "dir"),
        "nodes": len(nodes),
        "services": len(plan.services),
        "users": len(plan.users),
        "databases": len(plan.databases),
        "fake_secrets": len(plan.secrets),
        "bytes": total,
    }
    return rep
