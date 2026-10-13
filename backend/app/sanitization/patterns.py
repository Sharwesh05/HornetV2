"""Secret and private-data detection. One implementation shared by the sanitizer (real data in),
the AI document validator and the environment validator (generated data out)."""
import math
import re
from dataclasses import dataclass

PLACEHOLDER = "<REDACTED:{kind}>"
_NOT_REDACTED = r"(?!<REDACTED)"  # never re-match our own placeholders


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int


@dataclass(frozen=True)
class _Rule:
    kind: str
    regex: re.Pattern
    group: int = 0  # which group holds the sensitive part


def _r(kind: str, pattern: str, group: int = 0, flags: int = 0) -> _Rule:
    return _Rule(kind, re.compile(pattern, flags), group)


RULES: tuple[_Rule, ...] = (
    _r("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)?"),
    _r("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    _r("stripe_key", r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{8,}\b"),
    _r("github_token", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    _r("slack_token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    _r("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    # scheme://user:PASSWORD@host
    _r("url_credentials", r"\b[a-z][a-z0-9+.-]*://[^/\s:@]*:(" + _NOT_REDACTED + r"[^/\s@]+)@", 1, re.I),
    _r("bearer_token", r"\bbearer\s+(" + _NOT_REDACTED + r"[A-Za-z0-9._~+/=-]{16,})", 1, re.I),
    # password=..., "token": "...", API_KEY: ...
    _r(
        "kv_secret",
        r"\b(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|auth(?:orization)?)"
        r"\b[\"']?\s*[=:]\s*[\"']?(" + _NOT_REDACTED + r"[^\s\"',;&]{4,})",
        1,
        re.I,
    ),
    # --password hunter2 / --token=abc
    _r(
        "cli_secret",
        r"(?:^|\s)--?(?:pass(?:word)?|token|secret|api-?key)(?:=|\s+)(" + _NOT_REDACTED + r"[^\s\"']{4,})",
        1,
        re.I,
    ),
    # curl -u user:PASSWORD
    _r("cli_secret", r"(?:^|\s)-u\s+[^\s:]+:(" + _NOT_REDACTED + r"[^\s\"']{3,})", 1),
    _r("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
    _r("ipv4", r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
    _r("host_path", r"\b[A-Za-z]:\\[^\s\"']+|(?<![\w.])/(?:home|Users)/[A-Za-z0-9._-]+(?:/[^\s\"']*)?"),
)

# long random-looking tokens, found by entropy rather than shape
_ENTROPY_CANDIDATE = re.compile(r"(?<![A-Za-z0-9+/_=-])(?<!sha256:)[A-Za-z0-9+/_-]{32,}={0,2}(?![A-Za-z0-9+/_-])")

SECRET_KINDS = frozenset(
    {
        "private_key", "aws_access_key", "stripe_key", "github_token", "slack_token", "jwt",
        "url_credentials", "bearer_token", "kv_secret", "cli_secret", "high_entropy",
    }
)
ALLOWED_IPS = ("127.0.0.1", "0.0.0.0")  # noqa: S104  (not a bind; these literals are harmless to keep)


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = {c: s.count(c) for c in set(s)}
    return -sum(n / len(s) * math.log2(n / len(s)) for n in counts.values())


def _entropy_hits(text: str) -> list[Finding]:
    hits = []
    for m in _ENTROPY_CANDIDATE.finditer(text):
        tok = m.group(0)
        if tok.startswith("/") or tok.count("/") >= 2:  # a file path, not a token
            continue
        has_mix = any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok)
        if has_mix and shannon_entropy(tok) >= 3.8 and not re.fullmatch(r"[0-9a-f]{32}", tok):
            hits.append(Finding("high_entropy", m.start(), m.end()))
    return hits


def find_secrets(text: str, kinds: frozenset[str] | None = None) -> list[Finding]:
    """Return non-overlapping findings. `kinds=None` means every kind."""
    found: list[Finding] = []
    for rule in RULES:
        if kinds is not None and rule.kind not in kinds:
            continue
        for m in rule.regex.finditer(text):
            if rule.kind == "ipv4" and m.group(0) in ALLOWED_IPS:
                continue
            s, e = m.span(rule.group)
            if s >= 0:
                found.append(Finding(rule.kind, s, e))
    if kinds is None or "high_entropy" in kinds:
        found.extend(_entropy_hits(text))

    found.sort(key=lambda f: (f.start, -(f.end - f.start)))
    merged: list[Finding] = []
    for f in found:
        if merged and f.start < merged[-1].end:
            continue  # overlaps an earlier (longer or earlier) finding
        merged.append(f)
    return merged


def redact(text: str) -> tuple[str, list[Finding]]:
    findings = find_secrets(text)
    out, pos = [], 0
    for f in findings:
        out.append(text[pos : f.start])
        out.append(PLACEHOLDER.format(kind=f.kind))
        pos = f.end
    out.append(text[pos:])
    return "".join(out), findings


# an argv element that is a "give me a secret next" flag, e.g. ["--token", "<value>"]
SECRET_FLAG = re.compile(r"^--?(?:pass(?:word|wd)?|pwd|token|secret|api-?key|access-?key|auth)$", re.I)
