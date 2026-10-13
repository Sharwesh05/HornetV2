"""AI sanitization review (NVIDIA NIM or any AIProvider with `is_model = True`).

Order matters and is the safety argument:

  1. Deterministic sanitizer runs first. Env values were never collected; secrets, URLs with
     credentials, tokens, IPs, emails and host paths are already redacted from every string.
  2. THIS step sends only that already-redacted text to the model and asks it to flag what a regex
     cannot see: person and customer names, internal hostnames and URLs, organisation names,
     account identifiers, secrets in unfamiliar formats.
  3. The model can only REMOVE: it returns substrings that must appear verbatim in the text we sent,
     and we replace them. It cannot add, rewrite or move data. It can name env vars that look like
     secrets (those become 'secret'); it has no way to lower a class.
  4. assert_clean() runs again on the result, so the deterministic guarantee still holds if the model
     is wrong, down, or hostile.

The model's output is validated in ordinary code; prompts are not a security boundary.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from app.ai.provider import AIProvider
from app.profile import SanitizedProfile, ServiceSpec
from app.sanitization.sanitizer import SanitizationFailure, assert_clean, profile_hash

CATEGORIES = {"credential", "personal_data", "internal_host", "org_name", "identifier", "other"}
MIN_SUBSTRING, MAX_SUBSTRING = 3, 200
MAX_FINDINGS_PER_CALL = 100
MAX_REDACTED_FRACTION = 0.6  # a structural field (path/command) that would be mostly erased is rejected
MAX_PARALLEL_CALLS = 4

SYSTEM_POLICY = (
    "You are a data-sanitization reviewer for infrastructure metadata that is about to be used to build a "
    "decoy server. The text was already machine-redacted. Find anything that is STILL sensitive: credentials "
    "or tokens in unusual formats, personal names, phone numbers, customer or company names, internal "
    "hostnames, internal URLs, account or tenant identifiers. Return substrings copied EXACTLY from the "
    "given fields; never paraphrase, never invent text. Do NOT flag: ordinary technical terms, standard "
    "software names, ports, standard paths, existing <REDACTED:...> markers, or any term listed in "
    "'keep_terms' (the project and service names are intentionally kept). Separately, list the environment "
    "variable NAMES (names only; you never see values) that probably hold a secret. Be brief: return only "
    "real findings, and an empty list when there are none."
)

OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["findings", "secret_env_names"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["field_id", "substring", "category"],
                "properties": {
                    "field_id": {"type": "string"},
                    "substring": {"type": "string"},
                    "category": {"type": "string", "enum": sorted(CATEGORIES)},
                    "confidence": {"type": "number"},
                },
            },
        },
        "secret_env_names": {"type": "array", "items": {"type": "string"}},
    },
}


class _Finding(BaseModel):
    field_id: str
    substring: str
    category: str = "other"
    confidence: float | None = None


class _Response(BaseModel):
    findings: list[_Finding] = []
    secret_env_names: list[str] = []


@dataclass
class ReviewStats:
    provider: str
    mode: str
    calls: int = 0
    applied: int = 0
    rejected: int = 0
    env_raised: int = 0
    errors: int = 0
    by_category: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict:
        return {
            "requested": True, "provider": self.provider, "mode": self.mode, "calls": self.calls,
            "findings_applied": self.applied, "findings_rejected": self.rejected,
            "env_classes_raised": self.env_raised, "errors": self.errors, "by_category": dict(self.by_category),
        }


# ---- which fields the model may look at --------------------------------------------------------
@dataclass
class _Field:
    id: str
    kind: str
    text: str
    set: callable  # writes the cleaned text back
    guarded: bool = True  # structural fields (paths, commands) must not be wiped out; label values may be


def _fields(svc: ServiceSpec) -> list[_Field]:
    out: list[_Field] = []
    for attr in ("workdir", "user", "healthcheck"):
        val = getattr(svc, attr)
        if val:
            out.append(_Field(attr, attr, val, lambda v, a=attr: setattr(svc, a, v)))
    for attr in ("command", "entrypoint"):
        for i, val in enumerate(getattr(svc, attr)):
            out.append(_Field(f"{attr[:3]}{i}", f"{attr}_arg", val, lambda v, a=attr, j=i: getattr(svc, a).__setitem__(j, v)))
    for i, val in enumerate(svc.fs_hints):
        out.append(_Field(f"fs{i}", "path", val, lambda v, j=i: svc.fs_hints.__setitem__(j, v)))
    for i, m in enumerate(svc.mounts):
        out.append(_Field(f"mt{i}", "mount_destination", m.destination, lambda v, mm=m: setattr(mm, "destination", v)))
    for i, key in enumerate(list(svc.labels)):
        if key.startswith("com.docker.compose."):
            continue  # structural bookkeeping the builder relies on: never sent, never altered
        out.append(_Field(f"lv{i}", "label_value", svc.labels[key], lambda v, k=key: svc.labels.__setitem__(k, v), guarded=False))
    return out


def _safe_placeholder(category: str) -> str:
    # filesystem-safe (no ':' '<' '>') because some of these fields are later used as paths
    return f"redacted-{category}"


def _is_kept(substring: str, keep_terms: list[str]) -> bool:
    low = substring.lower()
    return any(len(t) >= 3 and (low in t.lower() or t.lower() in low and len(low) <= len(t) + 2) for t in keep_terms)


def _apply(fields: list[_Field], findings: list[_Finding], keep_terms: list[str], stats: ReviewStats) -> None:
    by_id = {f.id: f for f in fields}
    per_field: dict[str, list[tuple[str, str]]] = {}
    for f in findings[:MAX_FINDINGS_PER_CALL]:
        fld = by_id.get(f.field_id)
        sub = f.substring
        ok = (
            fld is not None
            and MIN_SUBSTRING <= len(sub) <= MAX_SUBSTRING
            and sub in fld.text                      # must be copied verbatim from what we sent
            and "REDACTED" not in sub.upper()
            and not sub.startswith("redacted-")
            and not _is_kept(sub, keep_terms)         # project/service names are the identity we imitate
            and (f.confidence is None or f.confidence >= 0.5)
        )
        if not ok:
            stats.rejected += 1
            continue
        per_field.setdefault(fld.id, []).append((sub, f.category if f.category in CATEGORIES else "other"))
    stats.rejected += max(len(findings) - MAX_FINDINGS_PER_CALL, 0)

    for fid, items in per_field.items():
        fld = by_id[fid]
        text, erased = fld.text, 0
        for sub, cat in sorted(items, key=lambda x: -len(x[0])):  # longest first so overlaps resolve cleanly
            if sub in text:
                erased += text.count(sub) * len(sub)
                text = text.replace(sub, _safe_placeholder(cat))
                stats.applied += 1
                stats.by_category[cat] += 1
        if fld.guarded and erased > MAX_REDACTED_FRACTION * len(fld.text):
            stats.applied -= len(items)
            stats.rejected += len(items)
            for _, cat in items:
                stats.by_category[cat] -= 1
            continue  # over-redaction guard: keep the deterministic result for this field
        fld.set(text)
        fld.text = text


def _ask(svc: ServiceSpec, keep_terms: list[str], provider: AIProvider) -> dict | None:
    """One model call for one service (runs in a worker thread; touches no shared state)."""
    fields = _fields(svc)
    if not fields and not svc.env_names:
        return None
    context = {
        "task": "sanitization_review",
        "service": svc.name,
        "keep_terms": keep_terms,
        "fields": [{"id": f.id, "kind": f.kind, "text": f.text} for f in fields],
        "env_names": svc.env_names,
    }
    return provider.generate_structured(system_policy=SYSTEM_POLICY, context=context, output_schema=OUTPUT_SCHEMA)


def review_profile(profile: SanitizedProfile, provider: AIProvider, mode: str) -> SanitizedProfile:
    """Return a new profile with the model's validated findings applied. mode: advisory | required."""
    if mode == "off":
        return profile
    out = profile.model_copy(deep=True)
    stats = ReviewStats(provider=f"{provider.name}:{provider.version}", mode=mode)
    keep_terms = sorted({out.source, *(s.name for s in out.services)})

    # model calls run in parallel (they dominate the time); results are applied in service order
    def call(svc):
        try:
            return _ask(svc, keep_terms, provider), None
        except Exception as exc:  # noqa: BLE001  (any failure of the model path is handled by mode below)
            return None, exc

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_CALLS) as pool:
        answers = list(pool.map(call, out.services))

    for svc, (raw, exc) in zip(out.services, answers, strict=True):
        if raw is None and exc is None:
            continue  # nothing to review for this service
        stats.calls += 1
        try:
            if exc is not None:
                raise exc
            resp = _Response.model_validate(raw)
        except Exception as e:  # noqa: BLE001
            stats.errors += 1
            if mode == "required":
                kind = type(e).__name__ if not isinstance(e, ValidationError) else "InvalidResponse"
                raise SanitizationFailure(f"AI sanitization review failed ({kind})") from None
            continue
        _apply(_fields(svc), resp.findings, keep_terms, stats)
        for name in resp.secret_env_names:
            # Only plain config variables are upgraded. A 'connection' variable (a URL that embeds a
            # credential) keeps its more specific class: the builder needs it to keep the URL consistent
            # with the database it points at (found in a live run, where the model flagged every URL).
            if svc.env_classes.get(name) == "config":
                svc.env_classes[name] = "secret"
                stats.env_raised += 1
    out.report.ai_review = stats.as_dict()
    out.profile_hash = profile_hash(out)
    assert_clean(out)  # the deterministic guarantee still holds after the model has had its say
    return out
