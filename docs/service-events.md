# Service-event contract

The fake services inside a twin (`twin-image/emulator/hornet_emulator.py`) append one JSON object per line to
`/run/hornet/events.jsonl`. Only the system user can read that file; the orchestrator collects it after each command
(`SessionManager._ingest_service_events`) and stores request events as `service.<proto>` session events, linked to the
service's graph node. Phase 5 (AI synthesis) reads the `unsupported` flag from these events.

This is the interface between the emulators and the rest of Hornet. Replacing an emulator, or later putting a real
service behind a protocol, only has to keep producing these lines. The contract is also written as code in
`backend/app/orchestration/service_events.py`, and `test_emulator.py` checks every line the emulator writes against it.

## Request events (one per attacker request)

| Field | Type | Required | Meaning |
|---|---|---|---|
| `ts` | string (ISO 8601, UTC, ms) | yes | When the request was handled |
| `service` | string | yes | Service name from the observed environment (e.g. `db`) |
| `proto` | `http` \| `postgres` \| `redis` | yes | Protocol |
| `action` | string | yes | `GET /api/x`, `sql`, `auth`, `extended-protocol`, a Redis command ... |
| `status` | int (http) or `ok` \| `error` \| `failed` | yes | Outcome |
| `unsupported` | bool | no (absent = false) | The emulator could not answer: the gap Phase 5 fills |
| `remote`, `user`, `database`, `sql`, `code`, `agent`, `body`, `app`, `auth` | string | no | Protocol details; `code` is the PostgreSQL SQLSTATE |

Rules:
- Attacker-supplied strings are truncated to 400 characters (+ `...`). Treat all of them as untrusted when displaying.
- Passwords are never written (AUTH arguments and PostgreSQL password messages are dropped).
- `unsupported` is set by PostgreSQL (syntax error `42601`, extended protocol), Redis (unknown command) and HTTP
  (unknown route, unknown table, unmodelled method; not for a missing row of a known table or a validation error).

## Lifecycle events

Lines without `proto`: `{"type": "ready", "services": [...]}` when the emulator is listening, and
`{"type": "emulator_error", "service": ..., "error": ...}` if one service could not start. They are used for start-up
checks and never stored as session events.
