"""Parse `find -printf` output from the observer and diff two filesystem snapshots."""
from app.runtime.driver import FsEntry

# Raw string: find(1) expands \t and \0 itself. A real NUL in argv would make execve fail.
FIND_FORMAT = r"%y\t%s\t%T@\t%p\0"


def parse_find(raw: bytes) -> list[FsEntry]:
    entries: list[FsEntry] = []
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        parts = rec.decode("utf-8", "replace").split("\t", 3)
        if len(parts) != 4:
            continue  # truncated or malformed record: ignore, never trust blindly
        kind, size, mtime, path = parts
        try:
            entries.append(FsEntry(path=path, kind=kind, size=int(size), mtime=float(mtime)))
        except ValueError:
            continue
    return entries


def diff(before: list[FsEntry], after: list[FsEntry]) -> list[dict]:
    old = {e.path: e for e in before}
    new = {e.path: e for e in after}
    changes: list[dict] = []
    for path, e in new.items():
        o = old.get(path)
        if o is None:
            changes.append({"path": path, "operation": "created", "kind": e.kind, "size": e.size})
        elif o.kind != e.kind or (e.kind != "d" and (o.size, o.mtime) != (e.size, e.mtime)):
            # a directory's own size/mtime changes whenever a child is added; the child is reported instead
            changes.append({"path": path, "operation": "modified", "kind": e.kind, "size": e.size})
    for path, o in old.items():
        if path not in new:
            changes.append({"path": path, "operation": "deleted", "kind": o.kind, "size": o.size})
    return sorted(changes, key=lambda c: c["path"])
