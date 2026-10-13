"""Plan -> static graph nodes (README section 4.3). Filesystem nodes mirror the image; logical nodes
(services, endpoints, users, databases) live under pseudo directories that exist only in the graph."""
import hashlib
import posixpath
from dataclasses import dataclass, field

from app.environment.plan import EnvPlan


@dataclass
class PlanNode:
    path: str
    parent_path: str | None
    node_type: str  # directory | file | service | endpoint | user | database
    permissions: str | None = None
    content_ref: str | None = None
    sensitivity: str = "public"
    meta: dict = field(default_factory=dict)


def plan_nodes(plan: EnvPlan) -> list[PlanNode]:
    nodes: list[PlanNode] = []
    for path, f in plan.files.items():
        parent = None if path == "/" else (posixpath.dirname(path) or "/")
        if f.kind == "dir":
            nodes.append(PlanNode(path, parent, "directory", f"{f.mode:04o}", None, f.sensitivity,
                                  {"uid": f.uid, "gid": f.gid, "in_base_image": f.exists_in_base}))
        else:
            data = (f.content or "").encode()
            nodes.append(PlanNode(path, parent, "file", f"{f.mode:04o}", f.image_path, f.sensitivity, {
                "uid": f.uid, "gid": f.gid, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "origin": f.origin,
            }))

    for pseudo in ("/services", "/users", "/databases"):
        nodes.append(PlanNode(pseudo, "/", "directory", "0755", None, "public", {"virtual": True}))
    for sp in plan.services:
        base = f"/services/{sp.name}"
        nodes.append(PlanNode(base, "/services", "service", None, None, "public", {
            "virtual": True, "role": sp.role, "image": sp.image, "ip": sp.ip, "user": sp.user,
            "config_paths": sp.config_paths, "log_paths": sp.log_paths}))
        for port, proto in sp.ports:
            nodes.append(PlanNode(f"{base}/{proto}-{port}", base, "endpoint", None, None, "public",
                                  {"virtual": True, "port": port, "protocol": proto}))
    for u in plan.users.values():
        nodes.append(PlanNode(f"/users/{u.name}", "/users", "user", None, None, "public", {
            "virtual": True, "uid": u.uid, "home": u.home, "kind": u.kind}))
    for d in plan.databases:
        nodes.append(PlanNode(f"/databases/{d.name}", "/databases", "database", None, None, "synthetic-sensitive", {
            "virtual": True, "engine": d.engine, "service": d.service, "owner": d.owner,
            "tables": {t: len(rows) for t, rows in d.tables.items()}}))
    return nodes
