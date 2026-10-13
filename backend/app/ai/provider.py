"""AI provider abstraction (README section 5.4).

Phase 3 uses it only to enrich a finished environment plan with extra documents. The provider gets a
sanitized, minimal context, can only return JSON, and its output is validated in ordinary code
(ai/doc_validator.py) before anything is used. A remote LLM provider is added in Phase 5; until then
the deterministic TemplateProvider means the whole system works offline.
"""
from typing import Protocol

AI_POLICY_VERSION = "phase3-docs-v1"

DOCUMENT_SCHEMA = {
    "type": "object",
    "required": ["documents"],
    "properties": {
        "documents": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "required": ["path", "content", "purpose"],
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "purpose": {"type": "string"}},
            },
        }
    },
}

SYSTEM_POLICY = (
    "You write realistic but entirely fictional operator files for a decoy server. Use only the service "
    "names, ports and people given in the context. Never output passwords, tokens, keys, real hostnames or "
    "real IP addresses. Reply with JSON matching the schema and nothing else."
)


class AIProvider(Protocol):
    name: str
    version: str
    is_model: bool  # True for a real model (it can review data); False for the offline template provider

    def generate_structured(self, *, system_policy: str, context: dict, output_schema: dict) -> dict: ...


class TemplateProvider:
    """Deterministic, offline stand-in: no model call, same input gives same output."""

    name = "template"
    version = "1"
    is_model = False

    def generate_structured(self, *, system_policy: str, context: dict, output_schema: dict) -> dict:
        if context.get("task") != "documents":
            return {}  # the offline provider proposes nothing else: callers fall back to their defaults
        services = context["services"]
        project = context["project"]
        logs = [p for s in services for p in s["log_paths"]]
        history = ["ls -la", "cd /var/www", "docker ps"]
        for s in services:
            if s["role"] in ("postgres", "mysql"):
                history.append(f"psql -h {s['name']} -U {context['db_name']} {context['db_name']}")
            if s["role"] == "redis":
                history.append(f"redis-cli -h {s['name']} ping")
        history += [f"tail -n 50 {p}" for p in logs[:3]] + ["history | tail", "exit"]
        notes = [f"# {project} ops notes", ""] + [f"- {s['name']} ({s['role']}) listens on {s['ports']}" for s in services]
        return {
            "documents": [
                {"path": "/home/user/.bash_history", "content": "\n".join(history) + "\n", "purpose": "shell history"},
                {"path": "/home/user/NOTES.txt", "content": "\n".join(notes) + "\n", "purpose": "operator notes"},
            ]
        }
