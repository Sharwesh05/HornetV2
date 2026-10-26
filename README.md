# Hornet v2: AI-Powered Digital Twin Honeypot

A honeypot for Docker environments. It observes an authorized real server, builds a sanitized and versioned synthetic copy (the *digital twin*), and runs attacker sessions inside isolated, disposable Linux environments. Real command execution gives realistic output. AI fills in only the gaps, such as fake services and unfamiliar application behavior. Every session is recorded and linked to a precomputed environment graph.

> **Core principle:** real server for observation → AI-assisted environment construction → isolated twin for execution → AI suggestions for the gaps, recorded for investigators (never served to attackers) → attack analytics.
> The production server is never the execution target for attacker input.

## Start here

| I want to... | Read |
|---|---|
| Install, run and use it (Linux; Windows notes) | [docs/user-guide.md](docs/user-guide.md) |
| Present it | [docs/demo-script.md](docs/demo-script.md) |
| Understand what is built and how | [architecture.md](architecture.md), then [READING_GUIDE.md](READING_GUIDE.md) for the code |
| Know what it protects against, and what not | [docs/threat-model.md](docs/threat-model.md), [docs/limitations.md](docs/limitations.md) |
| See the measurements and tests | [docs/evaluation-report.md](docs/evaluation-report.md), [docs/results/](docs/results/) |

```bash
python -m scripts.dev setup && python -m scripts.dev ui-setup     # once
python -m scripts.dev demo-up                                     # Docker running: base image + demo servers
python -m scripts.dev api                                         # terminal 1: API + SSH door (127.0.0.1:2222)
python -m scripts.dev ui                                          # terminal 2: dashboard (http://127.0.0.1:5173)
```

**Status:** Phases 0 to 8 are done (5A for AI synthesis). Phase H (hardening: gVisor, escape suite, PostgreSQL
guards, `prod` profile, deployment design) is done and awaiting approval. 604 backend and 64 dashboard tests pass.
Nothing is exposed beyond one machine until Phase H is approved.

> The sections from *Goals* to *Capstone scope* below are the **original design**. Where the build differs, notably
> that AI answers are recorded out of band and never served to attackers, that there is one hardened-Docker runtime,
> and that public deployment is not done, [architecture.md](architecture.md) describes what is actually built.

---

## Table of contents
1. [Goals](#goals)
2. [Architecture](#architecture)
3. [Key design decisions](#key-design-decisions)
4. [Execution and response model](#execution-and-response-model)
5. [Environment construction](#environment-construction)
6. [Data model](#data-model)
7. [API](#api)
8. [Dashboard](#dashboard)
9. [Security model](#security-model)
10. [Project structure](#project-structure)
11. [Tech stack](#tech-stack)
12. [Deployment](#deployment)
13. [Testing and acceptance criteria](#testing-and-acceptance-criteria)
14. [Roadmap](#roadmap)
15. [Capstone scope (MVP)](#capstone-scope-mvp)
16. [Related work and novelty](#related-work-and-novelty)
17. [Status](#status)

---

## Goals

**Functional**
- Discover Docker containers, services, ports and approved filesystem metadata.
- Build a static, versioned base environment graph before any attacker session starts.
- Generate synthetic config files, logs, users, database records and credentials.
- Run commands and scripts inside an isolated Linux environment.
- Capture stdout, stderr, exit codes, process activity and state changes.
- Use AI to produce consistent responses for simulated services and unsupported interactions.
- Keep independent state for every attacker session.
- Track interactions against the precomputed tree without rebuilding it live.
- Provide a dashboard with attack paths, session timelines and alerts.
- Prevent access to real secrets, production services and host resources.

**Non-functional**
- **Isolation:** no unrestricted path from the twin to production.
- **Consistency:** output reflects actual twin state wherever execution happens.
- **Auditability:** raw execution evidence is always distinguishable from AI-generated content.
- **Reproducibility:** versioned images, snapshots and response policies.
- **Scalability:** many independent sessions with resource limits.
- **Extensibility:** new service emulators and AI providers can be added without redesign.

---

## Architecture

The system has six logical planes:

| Plane | Responsibility |
|---|---|
| Observation | Read-only discovery of the real Docker environment |
| Environment construction | Sanitize metadata and generate the digital twin |
| Execution | Run attacker interactions in isolated, disposable environments |
| AI synthesis | Produce coherent synthetic behavior from verified observations |
| Security and telemetry | Enforce boundaries and record activity |
| Presentation | Admin dashboard and investigation tools |

```
Real Docker Server
   └─ Read-only Discovery Agent ─► Sanitization + Secret Scanner
                                      └─► Environment Builder ─► Versioned Base Graph
                                                                 + Pinned Twin Image
                                                                        │
Attacker ─► Honeypot Gateway ─► Session Manager ─► Orchestrator ─► Disposable Linux Twin
                                                                        │
                                                      Execution Observer (stdout/stderr/exit/state diff)
                                                                        │
                                          AI Synthesis (context builder → provider → validator)
                                                                        │
                                              Event Store (PostgreSQL) ─► API + WebSocket ─► React Dashboard
```

### Components

| # | Component | Role |
|---|---|---|
| 1 | Discovery Agent | Collects allowlisted Docker metadata, image IDs, port mappings, selected logs. Never exports unrestricted file contents or env vars. |
| 2 | Sanitization Engine | Classifies and removes secrets and private data; produces a sanitized server profile. |
| 3 | AI Environment Builder | Generates synthetic files, credentials, schemas, logs and relationships from the sanitized profile. |
| 4 | Environment Graph Manager | Validates and stores the immutable, versioned base tree. |
| 5 | Twin Orchestrator | Allocates, monitors, resets and destroys disposable runtimes; enforces limits and network isolation. |
| 6 | Execution Observer | Captures real execution results and keeps observations separate from inferences. |
| 7 | AI Response Engine | Constrained synthesis service for fake services and gaps. Not an agent. |
| 8 | Event and Analytics Engine | Records sessions, commands and outcomes, linked to the graph and the session overlay. |

Discovery, builder, execution and admin interfaces should be separate security domains even if the first implementation packages them together.

---

## Key design decisions

| Concern | Decision |
|---|---|
| Main server | Existing Docker server, observed read-only with authorization |
| Digital twin | Prepared, disposable Linux environment, one per session |
| Isolation | MicroVM or hardened sandbox (Firecracker, Kata or gVisor) |
| Base tree | Precomputed, versioned, immutable snapshot in PostgreSQL |
| Dynamic behavior | Real guest execution plus per-session overlay |
| Synthetic data | AI generation from sanitized metadata and validated templates |
| Output realism | Real guest output first, AI only for synthetic services and gaps |
| Backend / Frontend | Python + FastAPI / React + TypeScript |
| AI | Provider-agnostic adapter, optional local inference |

Digital twin = execution realism. AI = behavioral realism. Precomputed graph = structural consistency.

---

## Execution and response model

Commands go to the isolated runtime. The LLM never receives a raw command to run, and nothing the LLM returns is ever executed.

| Mode | Examples | Behavior |
|---|---|---|
| Real guest execution | `pwd`, `ls`, `cat`, `ps` | Run in the guest, return captured output unchanged |
| Synthetic service | Fake app API, database, cloud endpoint | Schemas, fixtures and deterministic state transitions |
| AI-assisted | Behavior not covered by fixtures | Structured proposal grounded in twin state, then validated |

**Response policy (in order)**
1. Actual command output takes precedence and is never rewritten.
2. Synthetic services follow defined fixtures and schemas.
3. AI fills gaps only where fixtures don't cover them.
4. Every AI output is validated: schema, resource references, consistency, sensitive data.
5. On timeout, invalid output or contradiction, use a predefined fallback or a controlled service error.
6. Raw observation and AI proposal are stored separately.

**Execution request** (limits come from server-side policy, never from the attacker):
```json
{
  "session_id": "session-123",
  "request_id": "req-456",
  "operation": "interactive_input",
  "input": "pwd",
  "snapshot_id": "snapshot-7",
  "timeout_ms": 5000,
  "output_limit_bytes": 65536
}
```

**Execution result** (evidence, not AI output):
```json
{
  "request_id": "req-456",
  "exit_code": 0,
  "stdout": "/var/www/app\n",
  "stderr": "",
  "timed_out": false,
  "truncated": false,
  "duration_ms": 4
}
```

**Structured AI proposal** (never trusted directly):
```json
{
  "response_mode": "synthetic_service",
  "source_observation_id": "obs-901",
  "status_code": 200,
  "body": { "customer_id": "cust_demo_104", "status": "active" },
  "state_changes": [],
  "evidence_refs": ["env-node-32"],
  "validation_required": true
}
```

The AI provider interface:
```python
class AIProvider(Protocol):
    async def generate_structured(
        self, *, system_policy: str, context: dict, output_schema: dict
    ) -> dict: ...
```
The synthesis service holds no runtime-control credentials and cannot invoke host commands. Attacker-provided content is labeled untrusted, history is minimized, and prompts are not a security boundary.

**Observation sources:** process supervisor, guest execution adapter, filesystem overlay diff, guest audit instrumentation, synthetic service logs, runtime controller. A file being opened does not prove its contents were read. Observations and AI classifications are stored separately.

---

## Environment construction

1. Collect approved container metadata and application structure.
2. Reject fields outside the discovery allowlist.
3. Remove secrets, credentials, private identifiers and unnecessary content.
4. Normalize into a canonical profile.
5. Generate synthetic files and data (deterministic templates plus AI).
6. Validate references between files, services, users and databases.
7. Build the base graph and a pinned twin image.
8. Run image, isolation and secret-scanning checks.
9. Publish the snapshot atomically, only if everything passed.

A failed rebuild never replaces an active snapshot. The previous validated version stays live until an admin approves a new one.

**Graph node fields:** `node_id`, `snapshot_id`, `parent_id`, `virtual_path`, `node_type` (directory, file, service, user, database, endpoint), `permissions`, `content_ref`, `sensitivity_class`, `metadata`.

**Graph invariants**
1. Every non-root node has a valid parent.
2. Paths are normalized and cannot escape the virtual root.
3. Every file and service reference resolves.
4. A published snapshot is immutable.
5. The session overlay cannot mutate the base snapshot.
6. AI output cannot add permanent base nodes at request time.

**Session lifecycle:** `Requested → Provisioning → Ready → Active → Quiescing → Archived → Destroyed` (or `Failed`). Each session has one snapshot, one isolated runtime, one policy, a bounded history, a state overlay and timestamped events. Runtimes are never shared between attackers. A reset destroys the runtime and recreates it from the pinned image.

---

## Data model

PostgreSQL with foreign keys, JSONB for bounded metadata, and indexes on timestamps, session IDs and snapshot versions.

| Entity | Purpose |
|---|---|
| `ENVIRONMENT_SNAPSHOT` | Version, status, profile hash |
| `VIRTUAL_NODE` | Static graph nodes (parent link, path, type, metadata) |
| `SERVICE_PROFILE` | Synthetic service type and behavior config |
| `TWIN_IMAGE` | Image digest and runtime type per snapshot |
| `HONEYPOT_SESSION` | Snapshot, runtime ID, status, start/end |
| `EXECUTION_REQUEST` | Operation and input hash per session |
| `EXECUTION_OBSERVATION` | Exit code and result metadata (evidence) |
| `AI_SYNTHESIS` | Response mode, validation status, metadata |
| `SESSION_EVENT` | Event type linked to session and graph node |
| `SESSION_OVERLAY` | Per-session virtual path operations |

**Storage rules**
- Snapshots are immutable once published.
- File contents stay in the guest unless explicitly approved for collection.
- Store bounded metadata and sanitized output, not unlimited raw payloads.
- Exclude sensitive values before they reach the LLM or telemetry.
- Record image digest, runtime version, snapshot ID and AI policy version for reproducibility.
- Keep admin audit events separate from attacker session events.

---

## API

Two separate surfaces with different authentication and network policy: the **admin API** and the **attacker-facing honeypot protocols**. The gateway never exposes admin endpoints.

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/v1/environments/build` | Start an authorized environment build |
| `GET` | `/api/v1/environments` | List snapshots |
| `GET` | `/api/v1/environments/{id}` | Snapshot metadata and validation status |
| `POST` | `/api/v1/environments/{id}/publish` | Publish a validated snapshot |
| `GET` | `/api/v1/environments/{id}/tree` | Precomputed base graph |
| `GET` | `/api/v1/sessions` | List sessions |
| `GET` | `/api/v1/sessions/{id}` | Session metadata |
| `GET` | `/api/v1/sessions/{id}/events` | Paginated event history |
| `GET` | `/api/v1/analytics/overview` | Event counts and summaries |
| `GET` | `/api/v1/health` | Health check |

**Internal runtime interface (private to the orchestrator):** `create_runtime`, `execute_input`, `collect_observation`, `terminate_runtime`, `destroy_runtime`. It accepts only validated IDs and server-controlled policies.

**WebSocket:** authenticated, authorized subscriptions only. Events include `session.event`, lifecycle changes, runtime-limit violations, synthetic service interactions and AI validation failures. Attackers can never subscribe to the admin stream.

---

## Dashboard

React + TypeScript + Tailwind CSS in `frontend/` (Phase 6). For investigators only: it talks to the admin API with
the admin key, which it keeps in the tab's memory (never in browser storage); a reload logs out.

```bash
python -m scripts.dev ui-setup     # once: npm ci (exact versions from frontend/package-lock.json)
python -m scripts.dev api          # the admin API (terminal 1)
python -m scripts.dev ui           # http://127.0.0.1:5173 (terminal 2); proxies /api to the API, loopback only
python -m scripts.dev ui-check     # lint, type check, tests, production build
```

Written for people who are not security specialists (Phase 6.5): plain-English wording throughout, a glossary
with hover/focus tooltips and a Help page, light and dark themes (follows the computer's setting, with a toggle),
self-hosted fonts (no outside requests), WCAG AA contrast (checked by a test that reads the colour tokens).

- **Home:** one sentence on what is happening now, the *hive* (one hexagon per recent visit: honey = live,
  red = stopped by a safety limit) and the live activity in plain English ("Logged in as root", "Queried the fake
  database"), typically within a second.
- **Attacker sessions:** every visit with a plain status ("Ended: the visitor disconnected"), duration, command and
  AI-suggestion counts; search and filters.
- **A visit:** a **"What happened"** summary built from the evidence (no AI): commands, files holding the decoy's
  fake passwords that were touched, files created, fake services used, AI suggestions kept, limits hit, why it
  ended. Then a **replay** (play, back, next) of the terminal as the visitor saw it, with two separate panels for
  the selected command: **What really happened** (evidence) and **AI suggestion** (never shown to the visitor).
  Further tabs: files touched, the environment map (visited / new or changed / holds fake passwords), all events.
- **Fake environments:** each version as a card with its status in words and one next step (Check it, Publish),
  and a build form that explains what building does and shows elapsed time.

**Attacker text is only ever rendered as text.** There is no HTML injection anywhere (enforced by ESLint rules), and
terminal escapes, control characters and bidirectional-override characters are shown visibly rather than
interpreted. XSS tests render hostile payloads through every component and fail if any element or handler appears.
Only the origins in `HORNET_DASHBOARD_ORIGINS` may call the API from a browser or open the live feed.

`python -m scripts.seed_demo_session` plays a fixed attacker session through the real gateway and exports it
(`frontend/src/test/fixtures/demo-session.json`); the dashboard's replay tests run on that recording.

---

## Security model

Security is enforced outside the AI model.

| Boundary | Main risks | Controls |
|---|---|---|
| Real server → discovery | Secret collection | Allowlist, least privilege, minimization |
| Builder → image | Poisoned or inconsistent data | Schema checks, secret scans, pinned images |
| Gateway → twin | Resource exhaustion | Rate limits, bounded input, deadlines |
| Guest → host | Escape, kernel exploits | Dedicated VM or hardened sandbox, minimal devices |
| Twin → network | Lateral movement, exfiltration | Default-deny network, restricted egress |
| Twin → observer | Forged logs | Treat output as untrusted, bounded capture |
| AI → response | Hallucination, prompt injection, leakage | Minimal context, schema and evidence validation, safe fallback |
| Event store → dashboard | Data exposure, stored XSS | Access control, output encoding, audit logging |

**Mandatory controls**
1. No Docker socket access from the twin.
2. No production secrets or real customer data in the twin image.
3. No host mounts, privileged containers or host networking for attacker workloads.
4. Default-deny networking; only explicit synthetic service connections.
5. Quotas, command deadlines, output caps and concurrency limits.
6. No AI-generated shell commands executed by the trusted host.
7. No automatic production IP blocking based solely on AI classification.
8. Immutable audit records for builds, runtime lifecycle and admin actions.
9. Versioned, integrity-verified images.
10. Regular security testing of the guest, sandbox, gateway, AI integration and dashboard.

No sandbox should be assumed invulnerable. For a public-facing deployment, run the twin runtime on a dedicated VM or host.

**Failure handling**

| Failure | Behavior |
|---|---|
| Discovery unavailable | Keep the last validated snapshot |
| Sanitization failure | Reject the build, record a security event |
| AI unavailable | Return raw execution output or a predefined fallback |
| AI output invalid | Discard it; no unbounded retries |
| Resource limit exceeded | Terminate or quarantine the runtime, record it |
| Snapshot or image mismatch | Refuse to launch |
| Network isolation violation | Terminate the session and alert |
| Database unavailable | Bounded buffering; never silently lose audit events |

Guest crash, execution timeout, synthesis timeout and infrastructure failure are tracked as distinct outcomes.

---

## Project structure

```
hornet_v2/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── api/            # admin, sessions, environments, events, websocket
│   │   ├── discovery/      # docker_inventory, metadata_collector, discovery_jobs
│   │   ├── sanitization/   # classifier, secret_redactor, policy
│   │   ├── environment/    # builder, graph, synthetic_data, validator
│   │   ├── orchestration/  # runtime_manager, session_manager, lifecycle
│   │   ├── observation/    # result_collector, audit_collector, state_changes
│   │   ├── ai/             # provider, context_builder, synthesizer, output_validator
│   │   ├── telemetry/      # event_service, alert_service
│   │   ├── db/  models/  config.py
│   └── tests/
├── frontend/
├── twin-image/
├── deployment/
└── docs/
```

Modules communicate through explicit interfaces, not each other's internal state. This is a code organization, not a requirement to deploy each package separately.

---

## Tech stack

| Layer | Technology |
|---|---|
| Management API | Python, FastAPI |
| Dashboard | React, TypeScript, Tailwind CSS |
| Real-time | WebSockets |
| Storage | PostgreSQL (JSONB plus relational parent-child links) |
| Events | Async queue first; durable broker at scale |
| Images | Versioned, scanned OCI images |
| Isolation | Firecracker or Kata (strong), gVisor or hardened Docker (practical) |
| AI | Provider-agnostic adapter, optional local model |
| Deployment | Docker Compose (management plane), separate sandbox runtime |
| Observability | Structured logs, metrics, health checks, audit events |

---

## Deployment

- Docker Compose for the management plane (API, DB, dashboard, discovery agent) during development.
- Attacker-facing workloads run in a separately managed VM or hardened sandbox.
- The public gateway is separate from the management API; it never exposes the DB, Docker control or AI credentials.
- Build guest images in a controlled pipeline and pin them by digest.
- Separate identities and credentials for discovery, management, AI inference and runtime control.
- Secrets are injected by a secret manager, never baked into images or source control.
- Validate configuration at startup; unsafe defaults must fail startup.
- Build one reusable validated image per snapshot and instantiate disposable copies. Do not clone production per session.

---

## Testing and acceptance criteria

**Test types:** unit (graph invariants, sanitization, path normalization, schemas), integration (end-to-end execution, observation, synthesis, persistence), isolation (host access, network escape, cross-session access, resource exhaustion, cleanup), AI consistency (prompt injection, invented files, inconsistent records, secret leakage) and performance (concurrency, latency, cleanup under load).

**First-release acceptance criteria**
- [ ] The base graph is generated before a session starts and is immutable during it.
- [ ] Commands run in the twin return captured execution results.
- [ ] Synthetic services produce schema-valid, consistent responses.
- [ ] AI-generated content is distinguishable from raw execution evidence.
- [ ] The environment contains no production credentials or unnecessary real user data.
- [ ] A guest cannot reach the production Docker socket, protected host paths or production services.
- [ ] Session state cannot leak between runtimes.
- [ ] Every execution request has a bounded deadline and output limit.
- [ ] Failed builds and validations never publish a snapshot.
- [ ] The dashboard can reconstruct the observed interaction sequence for an investigation.

---

## Roadmap

1. **Management foundation:** FastAPI, PostgreSQL, auth, config validation, snapshot and event data model.
2. **Digital twin execution:** minimal Linux guest, runtime controller, isolated command execution and capture. Validate isolation before accepting hostile input.
3. **Environment builder:** discovery, sanitization, synthetic data, graph, image preparation, snapshot publishing.
4. **Stateful deception:** session isolation, service fixtures, filesystem overlays, lifecycle management.
5. **AI synthesis:** provider abstraction, bounded context, structured output, consistency validation, fallback.
6. **Monitoring and dashboard:** tree explorer, visited-node overlays, session timelines, raw execution inspection, alerts.
7. **Security and performance:** sandbox boundary tests, secret handling, failure recovery, concurrency and retention, before any external exposure.

---

## Capstone scope (MVP)

The full design is larger than a single capstone. Suggested cut:

| Keep | Defer |
|---|---|
| Read-only Docker discovery plus allowlist sanitizer | Firecracker/Kata (use gVisor or hardened Docker) |
| Template plus LLM environment generator producing a versioned image | Many service emulators (one SSH service plus one fake web app or DB) |
| SSH front end with a per-session disposable container | Signed-image pipeline |
| LLM only for gaps, with schema validation and fallback | Advanced alerting and retention |
| Event store plus a simple timeline and tree dashboard | Multi-provider support (one or two providers) |

**What makes the evaluation convincing**
- Compare against baselines: plain Cowrie, a pure-LLM shell, and this twin. Measure session length, commands per session, hallucination or inconsistency rate, and fingerprinting-script detection.
- Show an isolation test suite (escape, egress, cross-session, resource exhaustion) that passes.
- Plant canary secrets in the "real" server and show none reach the twin or the LLM.
- Document an honest threat model, including what the sandbox does *not* protect against.

---

## Related work and novelty

Full report and raw notes: `reports/LLM honeypot digital twin prior work.md` and `research_notes/` (kept outside this repository). Per-paper and per-product limitations: [Research and Limitations.xlsx](Research%20and%20Limitations.xlsx).

The `reports/` and `research_notes/` folders are not part of this repository. Key references were re-checked against OpenAlex/arXiv on 2026-10-21 in [docs/evaluation-report.md](docs/evaluation-report.md) (section 8); note that "HoneyLLM" names two different 2024 papers.

No single published paper or product matches the whole pipeline, but most stages have a partial precedent. Search was limited, so "not found" is not proof of absence.

| Stage | Closest prior work | Gap relative to this project |
|---|---|---|
| Observe a live system and rebuild it as a honeypot | **PARALLAX** (Stony Brook, eCrime 2025): clones a LAMP app into Docker with LLM-synthesized data; HoneyPLC; HoneyFactory (network and services only) | Not read-only; web apps or network metadata only; no Docker-estate discovery |
| Digital-twin honeypot with sanitization | **TwinPot-Gen** (ICC 2026, abstract only); ICS/OT twins (SUTD, DiTwinIHon) | "Twin" is a network or plant model, not a container-level replica; mechanism unverified |
| LLM-generated environments | Chat2Pot, a Bologna thesis, honeytoken generators | Not built from a sanitized real profile; no cross-resource consistency checks |
| LLM shell honeypots | shelLM, VelLMes, HoneyGPT, LLMHoney, SBASH, LLM-SSHH, AdvancedShelLM, Cowrie LLM mode, Beelzebub, Galah | All emulate output; none runs commands for real in a per-session twin with the LLM only filling gaps |
| Real execution and disposable VMs | Cowrie proxy and Backend Pool (one VM per source IP), HoneyCloud, HoneySSH | No LLM, no sanitized environment builder, not per session |
| Commercial deception | Thinkst Canary, CounterCraft, Acalvio, FortiDeceptor, Zscaler | Closed source; CounterCraft markets AI "digital twins" but its mechanism is unpublished |

**What appears novel is the combination:** read-only Docker discovery, sanitization before any model sees data, an AI-generated versioned immutable environment, a per-session real-execution twin, validated LLM gap-filling, and raw evidence kept separate from AI output. Each ingredient alone is not new.

**Read in full before claiming novelty:** TwinPot-Gen, HoneyLLM, the SoK on honeypots and LLMs (arXiv 2510.25939), PARALLAX artifacts, AdvancedShelLM, LLM-SSHH, Honeyval.

**Evaluation implications from the literature**
- Include a real Ubuntu control; one study flagged real Ubuntu as a honeypot 70% of the time.
- Test against fingerprinting (Bitter Harvest-style probes) and an LLM attacker (Rouxii raised honeypot identification from 19% to 97%).
- Support non-interactive `ssh host cmd` sessions: 99.23% of authenticated sessions in one study were non-interactive.
- Measure secret leakage, escape and egress risk; prior work rarely reports them.

## Quick start

Needs Python 3.12+, Docker and Node 20+ (Linux, or Windows with Docker Desktop and WSL2). The full walkthrough is the
[user guide](docs/user-guide.md); the commands are the same on both systems.

```bash
python -m scripts.dev setup          # .venv, packages, .env with a random admin key
python -m scripts.dev safety         # the demo safety settings (refuses unsafe ones)
python -m scripts.dev test           # tests (Docker tests run when Docker and the demo servers are up)
python -m scripts.dev lint
```

`.env` lives in the repo root and is found from any working directory; the default SQLite file `hornet_dev.db` is
also in the repo root. API docs are at http://127.0.0.1:8000/docs (click **Authorize** and paste the key).

**Management plane in Docker (Postgres + API):** set `HORNET_ADMIN_API_KEY` and `POSTGRES_PASSWORD` in `.env`, then
`python -m scripts.dev up`. That container has no Docker access by design, so twins need the API run on the host.

**Demo servers (the "real" servers Hornet observes):** `python -m scripts.dev demo-up` starts a fake shop
(`shop-prod`: web app, Postgres, Redis on http://127.0.0.1:8080) and a fake help desk (`helpdesk-staging`). All data
and secrets in them are fake canaries, so tests can prove the sanitizer removes them.

## Phase 2: generic twin runtime

The runtime knows nothing about any particular application. A **snapshot** decides *what* the twin looks like (its image), a **driver** decides *how* it is isolated, and a **policy** decides every limit.

```
snapshot (published)  ->  TwinImage row (pinned digest)  ->  RuntimeDriver.create()
                                                                 |
admin/gateway -> SessionManager.execute(session, input) -> RuntimeDriver.exec()  -> ExecResult (raw evidence)
                         |                                       |
                         +-> ExecutionObservation (evidence)     +-> RuntimeDriver.snapshot_fs() -> fs diff -> SessionOverlay
                         +-> SessionEvent (timeline)
```

- **Generic base image:** `twin-image/Dockerfile` (Debian, no setuid bits, attacker uid 1000, PID 1 owned by a different uid). Phase 3 builds per-snapshot images `FROM` it. Build once: `docker build -t hornet-twin-base:2 twin-image`.
- **Driver interface:** `backend/app/runtime/driver.py`. `HardenedDockerDriver` (runc; Windows and Linux) and `GVisorDriver` (`runsc`, Linux; `HORNET_RUNTIME_DRIVER=gvisor`). Kata or Firecracker would plug in the same way.
- **Hardening applied to every twin:** no network, no mounts, no Docker socket, read-only root filesystem with size-limited tmpfs for `/tmp`, `/var/tmp` and the home directory, all capabilities dropped, `no-new-privileges`, default seccomp, memory/CPU/pids/open-file limits, no logging, `--init`.
- **Policy:** `backend/app/runtime/policy.py`. Timeouts, output caps and resource limits come from server settings (`HORNET_TWIN_*`, `HORNET_MAX_ACTIVE_SESSIONS`, `HORNET_SESSION_TTL_S`, `HORNET_IDLE_TTL_S`) and are bounded by hard maximums, so a bad config cannot silently weaken isolation. Attackers cannot change them.
- **Lifecycle:** requested, provisioning, ready, active, quiescing, destroyed, or failed. Idle and over-age sessions are reaped; a crashed orchestrator's orphan twins are removed on the next start; shutdown destroys all twins.
- **Evidence vs AI:** every exec result is stored as an `ExecutionObservation` marked `source: execution`. Nothing in Phase 2 involves an LLM.
- **Admin endpoints:** `POST /sessions`, `POST /sessions/{id}/exec`, `DELETE /sessions/{id}`. The exec endpoint is an admin test console; attackers will reach twins only through the Phase 4 gateway.

Run the isolation tests (need Docker and the base image; they are skipped otherwise):

```bash
.venv/Scripts/python -m pytest backend/tests/test_docker_isolation.py -v
```

**Limits of Phase 2 (by design, for later phases)**
- Each command is a separate process. Phase 4 added the persistent shell (working directory and exported variables carry over); a real PTY for interactive programs is still not available.
- Hardened Docker shares the host kernel (on Windows, the Docker Desktop VM's kernel). It is weaker than gVisor or a microVM, so do not expose it to the internet. For a server, use the gVisor driver on a dedicated Linux host (`deployment/README.md`).
- The orchestrator talks to Docker directly, so run the API on the host (not inside the compose `api` container, which has no Docker access by design) while developing this phase.
- A file an attacker hides inside a directory they `chmod 000` is not visible to the filesystem observer.
- A fork bomb cannot hurt the host (cgroup limits), but it leaves the twin unable to start processes. The manager
  detects a full process table (via `docker top`, which needs no fork) or `exit 125: fork failed`, marks the session
  `failed` ("limit exceeded: process limit") and destroys the twin. Found by running the bomb by hand and
  noticing the session stayed `active`; covered by regression tests.

## Phase 3: discovery, sanitization and environment builder

The observed server is an **input**, not an assumption. `main-server/` (a shop) and `demo-servers/helpdesk/`
(a ticket system) are two demos with different project names, services, images, ports, env vars, mounts and
business domains; the same code builds a twin for both, and for any other Docker environment you allowlist.

```
allowlisted compose project
  -> ReadOnlyDocker (4 read calls only; env var VALUES dropped at the source)
  -> deterministic sanitizer (regex: credentials, tokens, URLs with passwords, IPs, emails, host paths)
  -> AI sanitization review (NVIDIA NIM): flags what regexes cannot see; may only REMOVE or TIGHTEN
  -> final leak check (assert_clean)  ->  SanitizedProfile (the only thing stored or shown to an AI)
  -> data model (AI-proposed schema, validated; else keyword default; else neutral)
  -> role builders (postgres, mysql, redis, nginx, http-app, generic) + deterministic synthetic data
  -> optional AI documents (validated in code, never fatal)
  -> validator -> image build FROM hornet-twin-base:2 -> pinned image ID
  -> draft snapshot -> validated   (publishing stays an explicit admin step)
```

### AI sanitization with NVIDIA NIM

1. Put your key in `.env` yourself (it is git-ignored). Do not paste it in chat or commit it:
   ```
   HORNET_AI_PROVIDER=nim
   NVIDIA_API_KEY=nvapi-...
   HORNET_NIM_MODEL=meta/llama-3.1-70b-instruct     # any chat model on build.nvidia.com
   ```
   Startup fails if `nim` is selected without a key. The key is held as a secret value, sent only in the
   `Authorization` header, and never logged or included in errors.
2. **The model only ever sees text the deterministic sanitizer already redacted**, never environment values
   (those are not even collected). It returns substrings copied verbatim from that text; code validates every
   one (must exist verbatim, 3 to 200 chars, not a marker, confidence ok) and replaces them with
   `redacted-<category>`. It cannot add or rewrite data. It may raise an env var's class (`config` to
   `secret`), never lower it. Structural fields (paths, commands) are protected from being wiped out.
3. `HORNET_AI_SANITIZER_MODE`: `required` (default; the build fails if the model is unreachable or answers
   badly), `advisory` (record the failure, use the already-clean deterministic result) or `off`.
4. A final deterministic `assert_clean` runs after the model, so the guarantee holds even if the model is
   wrong, down or hostile. The report stores counts per category, never the removed text.
5. **Privacy:** with the hosted API, sanitized infrastructure metadata leaves your machine. For sensitive
   environments run a self-hosted NIM container and set `HORNET_NIM_BASE_URL` to it.

The same provider can also propose the database schema and operator documents; all of that is validated in
code and falls back to offline defaults if the model fails.

### Generic by construction

- **Roles** come from the image name, the command line and whether a port is published, not from a fixed port
  list. Unknown services get a `generic` twin. Add a role in `environment/roles.py` + `role_detect.py`.
- **Data** comes from a validated `DataModel` (tables and typed columns) proposed by the model or chosen by
  keywords (`commerce`, `helpdesk`, `content`, else a neutral accounts/audit model). Rows are generated from
  column types, so they match the fictional people used everywhere else.
- **App source stubs** follow hints in the profile (python, node, otherwise a plain config file).
- **Real directories** (workdir, log mounts) are used only if they are plain safe absolute paths.
- Two services of the same kind (two Redis, two Postgres) get separate files, users, credentials and databases.
- `test_generic_shapes.py` feeds the pipeline unusual shapes (no database, no ports, 25 mixed services, odd
  names and paths) and asserts a valid, leak-free environment; `test_docker_build.py` runs one test body
  against **both** demo servers.

### Other behaviour

- **Authorization:** only compose projects in `HORNET_DISCOVERY_ALLOWED_PROJECTS` (comma separated) can be
  observed; the default is none. Hornet's own twins are never profiled.
- **Read-only proof:** `ReadOnlyDocker` exposes `list_containers`, `inspect_container`, `container_diff`,
  `list_network_names`. The integration test checks no exec or restart happened in production containers.
- **Fake credentials** are consistent across files (app `.env`, init SQL, `~/.pgpass`, `redis.conf`).
- **Evidence vs AI:** every generated file is tagged `origin` (`template` or `ai:<provider>`).
- **Atomic builds:** a failed build leaves no snapshot, removes any half-built image, and audits
  `environment.build_failed` (plus `security.sanitization_failed`).
- **Seeded home:** the attacker's home and `/tmp` are tmpfs, so their files live under
  `/usr/share/hornet/seed` and are copied in at twin start (not counted as attacker changes).

Endpoints: `POST /api/v1/discovery/preview` (sanitized profile only) and `POST /api/v1/environments/build`
with `{"source": "docker", "project": "<compose project>"}`.

```bash
docker compose -f main-server/docker-compose.yml up -d            # demo server 1
docker compose -f demo-servers/helpdesk/docker-compose.yml up -d  # demo server 2
# set HORNET_DISCOVERY_ALLOWED_PROJECTS=shop-prod,helpdesk-staging in .env, then:
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{"source":"docker","project":"helpdesk-staging"}' http://127.0.0.1:8000/api/v1/environments/build
```

**Verified live against NVIDIA NIM (hosted API, `nvidia/nemotron-3-super-120b-a12b`)**
- A full build through the API for both demo servers: the model flagged a maintainer's name that no regex can
  catch, proposed a domain-appropriate database schema (5 tables for the helpdesk, 6 for the shop) that passed
  validation, and wrote operator documents; the code refused every document that would overwrite a generated
  file. No canary string reached the database, build folder or image.
- Things only a live run revealed, now fixed and covered by tests: the model's default (`meta/llama-3.1-70b-instruct`)
  returns HTTP 410 on the hosted API and several listed models return 404, so **check what your key can use**
  (`GET https://integrate.api.nvidia.com/v1/models`, then a one-line request); reasoning models can exhaust a small
  token budget mid-JSON (`HORNET_NIM_MAX_TOKENS`, default 4096, and a clear "output cut off" error); the model
  flagged the project name that the twin intentionally keeps (kept names are now sent as `keep_terms` and enforced in
  code); and it marked URL variables as secret-bearing, which would have broken URL/credential consistency (the
  model can now only upgrade plain `config` variables).
- Latency: a reasoning model makes a build take roughly 1.5 to 2 minutes (calls run in parallel). A smaller
  non-reasoning model is faster; model quality for this task is otherwise unmeasured beyond these two servers.

**Limits of Phase 3**
- The model's sensitivity judgement was checked on two small demo servers only; it also produces false positives
  (it flagged a demo label and part of a healthcheck as internal hosts), which are harmless but show it is not exact.
- The deterministic layer cannot know about names or internal hostnames; only the AI review catches those. In
  `off`/`template` mode such strings can remain in the stored (sanitized) profile.
- Phase 4 added running fake services (HTTP, PostgreSQL, Redis); other roles (e.g. MySQL) exist as files only.
- Discovery sees names, images, ports, mounts and `docker diff` paths, not file contents or env values, so
  generated configs are plausible rather than copies.
- Hostname and `/etc/hosts` are set by Docker at runtime. The attacker account is always `user`.
- The regex scanner is heuristic; keep the allowlist narrow and review the preview before building.

## Phase 4: SSH gateway, persistent shell and running services

```
ssh client -> HornetServer (auth policy, limits, refusals, GatewayEvent log)
                  -> one twin per connection (SessionManager)  -> each command = one recorded execution
                  -> persistent shell: working directory + exported variables carried between commands
twin (no network, loopback only) -> fake HTTP / PostgreSQL / Redis on 127.0.1.N, reachable by their original names
```

- **Start it:** set `HORNET_GATEWAY_ENABLED=true` in `.env` and run the API (`uvicorn app.main:app`). The gateway shares the
  API's session manager, listens on `127.0.0.1:2222` and refuses to bind elsewhere outside `HORNET_ENV=prod`. Connect with
  `ssh -p 2222 root@127.0.0.1`. It uses a stable ed25519 host key (created on first start under `data/gateway/`).
- **Login:** any username; a password is accepted on the Nth attempt of a connection (`HORNET_GATEWAY_ACCEPT_AFTER`) or at once if
  it is in `HORNET_GATEWAY_WEAK_PASSWORDS`. Public keys are refused but their fingerprints are logged. Every attempt is stored
  as a `GatewayEvent` (visible at `GET /api/v1/gateway/events`). By default only the password's length and a short hash
  are kept; `HORNET_GATEWAY_LOG_PASSWORDS=true` stores the attempted text (see *Command storage* below).
- **Sessions:** a connection gets a fresh twin when its first shell/exec channel opens and loses it when it closes. Reconnecting
  gives a clean twin; two connections never share one. Idle connections are logged out; a fork bomb or other limit violation
  ends the connection and the first cause is kept as the session's end reason.
- **Refused and logged:** SFTP/SCP and every other subsystem, direct/remote/unix port forwarding, agent/X11 forwarding.
  Per-address connection limit and twin capacity limit are enforced without breaking other connections.
- **Persistent shell, honestly:** every command still runs as its own process (so each is separate evidence), wrapped so the next
  one starts in the same directory with the same exported variables. The evidence records what was typed (not the
  wrapper), with credentials redacted; see *Command storage* below.
- **Running services:** services of the observed server get a fake inside the twin (`twin-image/emulator/hornet_emulator.py`,
  standard library only). `curl`, the real `psql` (including `\dt`) and `redis-cli` work against them, with the same fake
  credentials that are planted in `~/.pgpass`, `.env`, `redis.conf`. They share one in-memory store, so a row created through the
  HTTP API appears in SQL, and the state dies with the twin. Errors use real PostgreSQL / Redis codes and messages. Anything
  outside the supported subset gets a controlled error and an `unsupported` event, which the AI layer (Phase 5A) turns into
  an out-of-band proposal; see *AI synthesis* below.
- **Events linked to the graph:** commands that name a path in the file tree become `resource_access` events pointing at that node
  (labelled `basis: command-argument`: an observation, not proof of a read), file changes become `fs_change` events, and fake
  service requests become `service.http|postgres|redis` events linked to the service node. Passwords are never stored by the
  emulators; attacker-supplied text is truncated and must be escaped when displayed.

Verified with real clients: the gateway tests use an SSH client against the live server; `test_docker_gateway.py` connects to real
twins built from both demo servers and checks persistence, fake services, isolation between simultaneous connections, clean
reconnects, no route to the host or other sessions, fork-bomb handling, and that the emulator's config and events are unreadable
to the attacker. The whole flow was also run with a real OpenSSH 10.3 client.

**Limits of Phase 4 (be aware before showing this to a serious attacker)**
- No interactive programs (`vim`, `top`, `less`, password prompts), no aliases/functions/job control, and Ctrl-C does not interrupt a
  running command (it ends at completion or the timeout). Output is decoded as UTF-8 text, so binary output is corrupted.
- The login name is cosmetic: whatever username is tried, the shell is the unprivileged `user` account (`id` shows uid 1000).
- Fingerprinting: the SSH version banner imitates OpenSSH but the handshake is asyncssh; `uname -r` shows the Docker Desktop kernel
  (`...microsoft-standard-WSL2` on this machine); `ps` shows the emulator process; the emulator lives under `/usr/share/hornet`.
  A determined attacker or an AI pentester can tell. Stealth is out of scope for the demo; see `docs/limitations.md`.
  Under gVisor the tells differ (`uname -r` is `4.4.0`, `dmesg` names gVisor).
- Emulated: HTTP, PostgreSQL (simple-query protocol, md5 auth) and Redis only. MySQL and other services exist as files only; drivers
  that need PostgreSQL's extended protocol (prepared statements) get a clean error.
- A restart of the API destroys all twins and sessions are not resumable. Each connection is a new twin by design; an attacker who
  reconnects will not find yesterday's files.
- Hardened Docker on Windows shares the host VM kernel. Do not expose the gateway to the internet from a development machine.

### AI synthesis (Phase 5A)

When a fake service cannot answer a request (unknown SQL, `CLIENT LIST`, an unknown API route), the attacker gets
its controlled error, exactly as before. Afterwards, off the attacker's path, Hornet asks the model what a real
server would plausibly have answered, validates the answer in code and stores it in `ai_synthesis`, labelled
"AI proposal, not served to attacker". It never changes the recorded evidence.

```bash
HORNET_AI_SYNTH_ENABLED=true          # default false: nothing changes
HORNET_AI_PROVIDER=nim                # with `template` every gap is recorded as "fallback" (no model)
HORNET_NIM_SYNTH_MODEL=nvidia/nemotron-3-super-120b-a12b   # measured: ~1 s median (docs/results/synthesis_latency.md)
```

The model sees table and column names and the request (credentials redacted, marked untrusted), never data or
secrets. A proposal is rejected if it invents tables, columns or API resources, claims a write succeeded, or
contains secret-like strings, real email domains or real IP addresses. Statuses: `accepted`, `rejected`, `timeout`,
`fallback`. Results appear under `ai_proposals` in `GET /api/v1/sessions/{id}/executions`.

### Command storage

What an attacker types is research data, but it can contain real credentials (theirs, or a third party's they are
reusing). So it is stored with safeguards:

- **Stored by default:** `execution_request.input_text`, the command with credentials redacted (`password=...`,
  `DB_PASSWORD=...`, `--password x`, `-u user:pass`, URL credentials, bearer tokens, JWTs, cloud/API keys, private keys).
  Addresses, URLs, emails and encoded payloads are kept: they are the evidence. Capped at the input limit (8 KB).
  `input_hash` (SHA-256 of the raw input) is kept for matching.
- **Raw text:** only with `HORNET_STORE_RAW_COMMANDS=true` (`input_raw`; off by default, warned about in prod).
- **Login passwords:** length and a short hash by default; `HORNET_GATEWAY_LOG_PASSWORDS=true` stores the text.
- **Access:** admin API only (`X-API-Key`); never in `/health`, analytics or application logs.
- **Retention:** `HORNET_RETENTION_DAYS` (default 30). The purge (`app/retention.py`) deletes ended sessions older
  than that with everything recorded for them, plus old login attempts that never became a session. It is the only
  path allowed to delete evidence, and it writes a `retention.purge` audit event. Live sessions are never purged.
- **Known limit:** stdout and stderr are stored as-is, as execution evidence. A command that echoes a secret back
  (e.g. `echo $PASSWORD`) leaves it in the output.

## Status

- **Phase 1 (management foundation): done.**
- **Phase 2 (isolated twin execution): done.**
- **Phase 3 (discovery, sanitization, builder): done**, now generic and with an AI sanitization review via
  NVIDIA NIM. all tests pass on Docker Desktop (Windows), including a real-Docker test that runs the same
  assertions against two different demo servers and proves the planted canary secrets appear nowhere in the
  profile, build context, database, audit log or image.
- **NVIDIA NIM verified live** (see above), including key handling, schema-constrained output, and the full
  build for both demo servers.
- **Phase 4 (gateway, persistent shell, running services): done.** Verified with the test suite (including real-Docker
  tests through the SSH gateway for both demo servers) and by hand with a real OpenSSH client.
- **Phase 5A (AI synthesis, out of band): done.** Requests the fake services flag `unsupported` get an AI proposal,
  validated in code and stored apart from the evidence. It is not sent to the attacker (live serving, 5B, is
  optional and not built). See `architecture.md` section 10.
- **Phase 6 (dashboard): done.** Live feed, session replay, raw-vs-AI inspector, environment tree, environment
  management; XSS-safe rendering tested. **Phase 6.5:** redesigned for non-technical users (plain English,
  "What happened" summaries, glossary, light/dark). See *Dashboard* above.
- **Phase 7 (demo security and essential evaluation): done.** No egress by any protocol, cross-session isolation of
  files, processes and fake-service state, canary secrets absent from every model prompt, log and table, a startup
  check that refuses unsafe demo settings (`python -m scripts.dev safety`), runtime measurements
  (`docs/results/evaluation.md`) and the limitations table (`docs/limitations.md`).
- **Phase 8 (documentation): done.** User guide, demo script (rehearsed end to end), threat model, evaluation
  report with re-checked related work; architecture rewritten to match the build. See *Start here*.
- **Phase H (hardening): done, awaiting approval.** gVisor driver; escape and abuse suite on runc and gVisor;
  concurrency, restart and reaper tests on real Docker; PostgreSQL 16 migrations both ways with database triggers;
  audited admin console and a strict `prod` profile (secret files, automatic retention); mypy; `deployment/`.
  Results: [docs/threat-model.md](docs/threat-model.md) section 7.
