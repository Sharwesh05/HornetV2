"""Virtual path normalization and graph invariants (README section 4.3)."""
import posixpath
from dataclasses import dataclass


class PathError(ValueError):
    pass


def normalize_path(path: str) -> str:
    """Return a canonical absolute virtual path, rejecting anything that could escape '/'."""
    if not isinstance(path, str) or not path:
        raise PathError("empty path")
    if "\x00" in path or "\\" in path:
        raise PathError("illegal character in path")
    if not path.startswith("/"):
        raise PathError("path must be absolute")
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise PathError("path escapes the virtual root")
            parts.pop()
        else:
            parts.append(part)
    return "/" + "/".join(parts)


@dataclass(frozen=True)
class NodeSpec:
    key: str  # id within this validation run
    parent_key: str | None
    path: str
    node_type: str


def validate_tree(nodes: list[NodeSpec]) -> list[str]:
    """Return a list of invariant violations; an empty list means the tree is valid."""
    errors: list[str] = []
    by_key = {n.key: n for n in nodes}
    if len(by_key) != len(nodes):
        errors.append("duplicate node ids")

    roots = [n for n in nodes if n.parent_key is None]
    if len(roots) != 1:
        errors.append(f"expected exactly one root, found {len(roots)}")
    elif roots[0].path != "/":
        errors.append("root node must have path '/'")

    seen_paths: set[str] = set()
    for n in nodes:
        try:
            canonical = normalize_path(n.path)
        except PathError as exc:
            errors.append(f"{n.path!r}: {exc}")
            continue
        if canonical != n.path:
            errors.append(f"{n.path!r} is not normalized (expected {canonical!r})")
        if n.path in seen_paths:
            errors.append(f"duplicate path {n.path!r}")
        seen_paths.add(n.path)

        if n.parent_key is None:
            continue
        parent = by_key.get(n.parent_key)
        if parent is None:
            errors.append(f"{n.path!r}: parent does not exist")
        elif posixpath.dirname(n.path) != parent.path:
            errors.append(f"{n.path!r}: path is not a child of parent {parent.path!r}")
        elif parent.node_type != "directory" and not (parent.node_type == "service" and n.node_type == "endpoint"):
            errors.append(f"{n.path!r}: parent {parent.path!r} is not a directory")

    # cycle check (should be impossible when paths are consistent, but guard anyway)
    for n in nodes:
        hops, cur = 0, n
        while cur.parent_key is not None and hops <= len(nodes):
            cur = by_key.get(cur.parent_key, cur)
            hops += 1
            if cur.parent_key is None:
                break
        else:
            if cur.parent_key is not None:
                errors.append(f"cycle detected at {n.path!r}")
                break
    return errors
