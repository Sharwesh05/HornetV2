"""Validate AI-proposed documents in ordinary code. Nothing the model says is trusted: every rule
below is enforced regardless of what the prompt asked for."""
import re
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from app.environment.plan import MAX_FILE_BYTES, EnvPlan
from app.graph import PathError, normalize_path
from app.sanitization.patterns import SECRET_KINDS, find_secrets

ALLOWED_PREFIXES = ("/home/user/", "/opt/", "/srv/", "/var/www/", "/var/log/")
PROTECTED_PREFIXES = ("/etc/passwd", "/etc/group", "/etc/shadow", "/etc/sudoers", "/usr/share/hornet")
MAX_DOCS = 12
_ALLOWED_IP = re.compile(r"\b(?:203\.0\.113|198\.51\.100|10\.20\.0)\.\d{1,3}\b")
_ANY_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


class AIDocument(BaseModel):
    path: str
    content: str
    purpose: str = ""


@dataclass
class DocResult:
    accepted: list[AIDocument]
    rejected: list[str]  # reasons only, never the rejected content


def _check(doc: AIDocument, plan: EnvPlan, forbidden: tuple[str, ...]) -> str | None:
    try:
        path = normalize_path(doc.path)
    except PathError:
        return "invalid path"
    if path != doc.path:  # '..', '//' and '.' segments are never legitimate from a model
        return "path not allowed"
    if not path.startswith(ALLOWED_PREFIXES) or path.startswith(PROTECTED_PREFIXES):
        return "path not allowed"
    if path in plan.files:
        return "would overwrite a generated file"
    if len(doc.content.encode()) > MAX_FILE_BYTES or "\x00" in doc.content:
        return "content too large or binary"
    known = plan.secrets.values()
    scrubbed = doc.content
    for v in known:
        scrubbed = scrubbed.replace(v, "<REDACTED:fake>")
    if any(f.kind in SECRET_KINDS for f in find_secrets(scrubbed)):
        return "contains secret-like data"
    if any(n and n in doc.content for n in forbidden):
        return "contains forbidden value"
    for ip in _ANY_IP.findall(doc.content):
        if not (_ALLOWED_IP.fullmatch(ip) or ip in ("127.0.0.1", "0.0.0.0")):  # noqa: S104
            return "contains a non-synthetic IP address"
    allowed_emails = {p.email for p in plan.people}
    if any(e not in allowed_emails for e in _EMAIL.findall(doc.content)):
        return "contains an unknown email address"
    return None


def validate_documents(raw: object, plan: EnvPlan, forbidden: tuple[str, ...] = ()) -> DocResult:
    accepted: list[AIDocument] = []
    rejected: list[str] = []
    docs = raw.get("documents") if isinstance(raw, dict) else None
    if not isinstance(docs, list):
        return DocResult([], ["response does not match the schema"])
    seen: set[str] = set()
    for item in docs[:MAX_DOCS]:
        try:
            doc = AIDocument.model_validate(item)
        except ValidationError:
            rejected.append("document does not match the schema")
            continue
        reason = _check(doc, plan, forbidden)
        if reason is None:
            try:
                norm = normalize_path(doc.path)
            except PathError:  # pragma: no cover  (checked above)
                continue
            if norm in seen:
                reason = "duplicate path"
            else:
                seen.add(norm)
                accepted.append(AIDocument(path=norm, content=doc.content, purpose=doc.purpose))
        if reason:
            rejected.append(reason)
    if len(docs) > MAX_DOCS:
        rejected.append(f"{len(docs) - MAX_DOCS} documents over the limit")
    return DocResult(accepted, rejected)
