"""The service-event contract: what the in-twin service emulators write to /run/hornet/events.jsonl and what the
orchestrator ingests (SessionManager._ingest_service_events) and Phase 5 consumes. Documented in
docs/service-events.md. The emulator is standard-library only and cannot import this module, so the contract is
enforced by tests (test_emulator.py) on both sides.

Two kinds of line:
  * request events: one per attacker request to a fake service (have `proto`)
  * lifecycle events: emulator status such as {"type": "ready"} (have `type`, no `proto`); never stored as session
    events
"""
from typing import Any

PROTOCOLS = frozenset({"http", "postgres", "redis"})
# field -> allowed Python types. `status` is an HTTP code for http, and "ok" / "error" / "failed" otherwise.
REQUIRED: dict[str, tuple[type, ...]] = {"ts": (str,), "service": (str,), "proto": (str,), "action": (str,),
                                         "status": (int, str)}
# `unsupported`: the emulator could not answer this request (absent means False). This is the Phase 5 hook.
OPTIONAL: dict[str, tuple[type, ...]] = {
    "unsupported": (bool,), "remote": (str,), "user": (str,), "database": (str,), "sql": (str,), "code": (str,),
    "agent": (str,), "body": (str,), "app": (str,), "auth": (str, type(None)),
}
LIFECYCLE_TYPES = frozenset({"ready", "emulator_error"})
TEXT_LIMIT = 403  # emulator MAX_TEXT (400) + "..."


def is_request(event: dict[str, Any]) -> bool:
    return "proto" in event


def contract_problems(event: dict[str, Any]) -> list[str]:
    """Empty if `event` satisfies the contract; otherwise what is wrong with it."""
    if not is_request(event):
        return [] if event.get("type") in LIFECYCLE_TYPES else [f"unknown lifecycle line: {event.get('type')!r}"]
    problems = []
    for name, types in REQUIRED.items():
        if name not in event:
            problems.append(f"missing {name}")
        elif not isinstance(event[name], types) or isinstance(event[name], bool):
            problems.append(f"{name} has type {type(event[name]).__name__}")
    if event.get("proto") not in PROTOCOLS:
        problems.append(f"unknown proto {event.get('proto')!r}")
    for name, value in event.items():
        if name in REQUIRED:
            continue
        if name not in OPTIONAL:
            problems.append(f"unexpected field {name}")
        elif not isinstance(value, OPTIONAL[name]):
            problems.append(f"{name} has type {type(value).__name__}")
        elif isinstance(value, str) and len(value) > TEXT_LIMIT:
            problems.append(f"{name} is not truncated")
    return problems
