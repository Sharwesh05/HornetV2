"""Link what an attacker did to nodes of the static graph.

These links are OBSERVATIONS, not proof: a command that names `/etc/passwd` does not prove the file was
read, only that the attacker pointed at it. Events are labelled with their basis ("command-argument" or
"filesystem-diff") so the dashboard can show them as visited markers, never as confirmed compromise.
"""
import fnmatch
import posixpath
import re
import shlex

MAX_TOKENS = 200
MAX_LINKS = 20
_REDIRECT = re.compile(r"^(?:\d*[<>]{1,2}|&>)+")
_GLOB = re.compile(r"[*?\[]")


def _tokens(command: str) -> list[str]:
    try:
        toks = shlex.split(command, posix=True)
    except ValueError:  # unbalanced quotes etc.
        toks = re.findall(r"\S+", command)
    return toks[:MAX_TOKENS]


def touched_paths(command: str, cwd: str, home: str, known: set[str]) -> list[str]:
    """Absolute virtual paths, among `known` node paths, that the command's arguments point at."""
    found: list[str] = []
    for tok in _tokens(command):
        tok = _REDIRECT.sub("", tok)
        if not tok or tok.startswith("-") and "/" not in tok:
            continue
        if tok == "~" or tok.startswith("~/"):
            tok = home + tok[1:]
        path = posixpath.normpath(tok if tok.startswith("/") else posixpath.join(cwd, tok))
        candidates = [path] if not _GLOB.search(path) else sorted(p for p in known if fnmatch.fnmatchcase(p, path))[:5]
        for c in candidates:
            if c in known and c not in found:
                found.append(c)
        if len(found) >= MAX_LINKS:
            break
    return found
