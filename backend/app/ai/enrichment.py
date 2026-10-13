"""Ask the AI provider for extra documents, validate them, and add the accepted ones to the plan.
A provider failure or a rejected document never fails the build: the plan is complete without them."""
from dataclasses import dataclass, field

from app.ai.doc_validator import validate_documents
from app.ai.provider import AI_POLICY_VERSION, DOCUMENT_SCHEMA, SYSTEM_POLICY, AIProvider
from app.environment.plan import EnvPlan
from app.profile import SanitizedProfile


@dataclass
class EnrichmentReport:
    provider: str
    policy_version: str = AI_POLICY_VERSION
    requested: bool = False
    accepted: int = 0
    rejected_reasons: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def build_context(plan: EnvPlan, profile: SanitizedProfile, db_name: str) -> dict:
    """Minimal context: sanitized facts and fictional people only. No env values, no fake secrets."""
    return {
        "task": "documents",
        "project": profile.source,
        "db_name": db_name,
        "services": [
            {"name": s.name, "role": s.role, "ports": [p for p, _ in s.ports], "log_paths": s.log_paths}
            for s in plan.services
        ],
        "people": [p.full_name for p in plan.people],
        "allowed_prefixes": ["/home/user/", "/opt/", "/srv/", "/var/www/", "/var/log/"],
    }


def enrich(plan: EnvPlan, profile: SanitizedProfile, provider: AIProvider, db_name: str,
           forbidden: tuple[str, ...] = ()) -> EnrichmentReport:
    report = EnrichmentReport(provider=f"{provider.name}:{provider.version}", requested=True)
    try:
        raw = provider.generate_structured(
            system_policy=SYSTEM_POLICY, context=build_context(plan, profile, db_name), output_schema=DOCUMENT_SCHEMA
        )
    except Exception as exc:  # noqa: BLE001  (a model outage must not break a build)
        report.error = type(exc).__name__
        return report
    result = validate_documents(raw, plan, forbidden)
    for doc in result.accepted:
        uid = 1000 if doc.path.startswith("/home/user/") else 0
        plan.add_file(doc.path, doc.content, 0o644 if uid == 0 else 0o600, uid, uid,
                      origin=f"ai:{provider.name}", sensitivity="synthetic-sensitive" if uid else "public")
    report.accepted = len(result.accepted)
    report.rejected_reasons = result.rejected
    return report
