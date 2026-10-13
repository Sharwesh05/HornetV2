# AGENTS.md: Building Hornet v2

Instructions for AI coding agents (and humans) working on this repo. Read [README.md](README.md) first for the architecture. This file says **how to build it, in what order, and what you must never do**.

## 1. What we are building

An AI-powered digital twin honeypot for Docker environments:
read-only discovery → sanitization → AI-generated synthetic environment (versioned, immutable) → per-session disposable Linux twin that runs attacker commands for real → LLM fills only validated gaps → events and attack-path dashboard.

Background research: [reports/LLM honeypot digital twin prior work.md](reports/LLM%20honeypot%20digital%20twin%20prior%20work.md) and [Research and Limitations.xlsx](Research%20and%20Limitations.xlsx). Nearest prior art to keep in mind: PARALLAX, TwinPot-Gen, Cowrie proxy mode, LLM shell honeypots.

## 2. Non-negotiable rules

1. **Attacker input never runs on the host.** It runs only inside a disposable sandbox container/VM created by the orchestrator.
2. **The twin never gets the Docker socket,** host mounts, privileged mode, host networking, or production credentials. Egress is default-deny.
3. **The LLM never executes anything and never sees secrets.** Sanitize first, then build context. Treat all attacker text as untrusted data.
4. **Raw execution evidence and AI output are stored and displayed separately.** Never rewrite real command output with AI text.
5. **Every AI output is validated in ordinary code** (schema, references, consistency, sensitive data) with a safe fallback.
6. **Snapshots are immutable once published;** the session overlay never mutates the base graph.
7. **Limits come from server-side policy,** never from attacker input (timeout, output cap, CPU/memory/pids, session count).
8. **No secrets in the repo, images, logs or tests.** Use `.env` (git-ignored) and `.env.example`. Tests use fake canary values.
9. Discovery is read-only and allowlisted. Do not point it at systems you are not authorized to observe.
10. Do not run the honeypot on a public interface from a dev machine. Public exposure is Phase 7 only, on a dedicated Linux VPS.

11. **The observed server is an input, never an assumption.** Code outside tests and `main-server/` /
    `demo-servers/` must not mention a particular demo (names, tables, ports, frameworks). Every new
    behaviour is exercised against more than one server shape (`test_generic_shapes.py`, the two-stack
    Docker test).
12. **AI sanitizer (NVIDIA NIM):** deterministic redaction always runs first and always has the last word
    (`assert_clean`). The model only receives already-redacted text, can only remove or tighten, and its
    output is validated in code. API keys live in `.env` only.

If a task seems to need breaking a rule, stop and ask.

## 3. Cross-platform requirements (Windows first)

The primary dev machine is **Windows 11**. Everything must also run on Linux (and macOS where practical).

- **Python 3.12 or 3.13, pinned in `pyproject.toml`.** The current `.venv` uses 3.14; recreate it with 3.12 if a dependency lacks wheels.
- Use `pathlib.Path`, never hard-coded `/` or `\` separators. No shell-specific code in app logic (`subprocess` with argument lists, never `shell=True`).
- Open files with explicit `encoding="utf-8"`; write LF line endings (`.gitattributes`: `* text=auto eol=lf`, except `*.ps1`/`*.bat`).
- **Task runner:** a cross-platform entry point, `python -m scripts.dev <command>` (or `just` with PowerShell as `windows-shell`). Provide PowerShell (`.ps1`) and POSIX (`.sh`) wrappers only as thin shims.
- **Containers:** Docker Desktop with WSL2 on Windows; native Docker Engine on Linux. Talk to Docker through the Python `docker` SDK, never through shell strings. Detect the socket (`npipe:////./pipe/docker_engine` on Windows, `/var/run/docker.sock` on Linux).
- **Sandbox runtime is pluggable:** `runc` hardened (dev, Windows/Linux), `runsc` gVisor (Linux), Firecracker/Kata microVM (Linux with KVM only). The `RuntimeDriver` interface hides the difference; Windows dev uses the hardened-Docker driver and the docs must say it is weaker.
- Database: PostgreSQL via Docker Compose. Use SQLAlchemy 2 + Alembic. Keep JSONB use behind a small helper so SQLite-based unit tests still work.
- Frontend: Node 20+, Vite, React, TypeScript, Tailwind. Use `npm` scripts only; no bash-isms in `package.json`.
- CI matrix: `windows-latest` and `ubuntu-latest` for unit and integration tests. Isolation tests run on Linux only.

## 4. Repository layout

```
hornet_v2/
├── AGENTS.md  README.md  pyproject.toml  .env.example  docker-compose.yml
├── backend/app/   (api, discovery, sanitization, environment, orchestration,
│                   observation, ai, telemetry, db, models, config.py, main.py)
├── backend/tests/ (unit, integration, isolation, ai_consistency)
├── frontend/
├── twin-image/    (Dockerfile + build scripts for the pinned guest image)
├── gateway/       (SSH front end that forwards to sessions)
├── deployment/    (compose files, VPS/Terraform notes, network policies)
├── scripts/       (dev tooling, make_papers_xlsx.py)
├── docs/  reports/  research_notes/
```

Modules talk through explicit interfaces (`RuntimeDriver`, `AIProvider`, `Sanitizer`, `EventSink`), not each other's internals.

## 5. Working conventions

- Small, reviewable changes; one phase deliverable at a time. Update docs with the code.
- Type hints everywhere; `ruff` + `mypy` (strict on `sanitization`, `ai`, `orchestration`). Frontend: `eslint` + `tsc --noEmit`.
- Tests first for security-relevant code (path normalization, sanitizer, validator, policy). Every bug gets a regression test.
- Config via a single validated settings object (`pydantic-settings`). **Unsafe defaults must fail startup.**
- Structured JSON logs. Admin audit events and attacker session events go to separate tables/streams.
- Commit messages: imperative, scoped (`orchestration: enforce pids limit`).
- Prefer fakes over mocks for the AI provider (`FakeProvider` with canned and adversarial outputs) so tests are deterministic and offline.

## 6. Phases

Each phase has a goal, deliverables, and **exit criteria**. Do not start the next phase until the exit criteria pass on both Windows and Linux (except where marked Linux-only).

### Phase 0: Scaffolding and tooling
**Goal:** a repo anyone can clone and run on Windows or Linux in minutes.
- `pyproject.toml`, `.env.example`, `.gitignore`, `.gitattributes`, pre-commit (ruff, mypy).
- `scripts/dev.py` with `setup`, `test`, `lint`, `up`, `down`, `migrate`.
- `docker-compose.yml` (Postgres first), CI for Windows + Linux.
- Config loader that rejects unsafe defaults.

**Exit:** `python -m scripts.dev setup && python -m scripts.dev test` passes on a clean Windows and a clean Linux machine in CI.

### Phase 1: Management foundation
**Goal:** the API, auth and data model that everything else hangs on.
- FastAPI app, health endpoint, admin auth (API key or JWT), RBAC for admin only.
- Alembic migrations for: `environment_snapshot`, `virtual_node`, `service_profile`, `twin_image`, `honeypot_session`, `execution_request`, `execution_observation`, `ai_synthesis`, `session_event`, `session_overlay`, `admin_audit_event`.
- Event service with append-only audit semantics.
- Endpoints: environments (list/get/tree), sessions (list/get/events), analytics overview.

**Exit:** OpenAPI docs render; migrations apply and roll back; snapshot rows cannot be updated after `published` (enforced and tested).

### Phase 2: Isolated twin execution (core of the project)
**Goal:** run a command in a disposable sandbox and capture real evidence.
- `twin-image/`: minimal pinned Linux image (non-root user, no package manager access for the attacker, read-only rootfs + tmpfs workdir).
- `RuntimeDriver` interface + `HardenedDockerDriver` (drop all caps, `no-new-privileges`, seccomp default, pids/memory/CPU/disk limits, no network, no mounts). Add `GVisorDriver` (Linux).
- Orchestrator lifecycle: Requested → Provisioning → Ready → Active → Quiescing → Archived → Destroyed/Failed.
- Execution request/result contract (timeout and output cap from policy only), cancellation, cleanup on timeout/crash.
- Observer: stdout, stderr, exit code, duration, truncation, filesystem diff (overlay) and process list.

**Exit:** `pwd`, `ls`, `cat`, `ps`, a fork bomb, a 1 GB write, and an infinite loop all behave safely; runtimes are destroyed; no host path or Docker socket is reachable (isolation tests, Linux required for the full suite).

### Phase 3: Discovery, sanitization and environment builder
**Goal:** turn an authorized real Docker host into a versioned synthetic snapshot.
- Read-only discovery agent using the Docker SDK against an **allowlist** (containers, images, ports, networks, labels; no env values, no file contents by default).
- Sanitizer: classify and redact env vars, labels, mounts, compose secrets, tokens, keys, emails, IPs. Canary-secret tests prove nothing leaks.
- Builder: deterministic templates first; AI generation (via `AIProvider`) of files, users, logs, DB records, fake creds from the **sanitized** profile only.
- Validator: cross-resource consistency (a user in `/etc/passwd` exists in DB, logs and creds), graph invariants (valid parents, normalized paths, resolvable refs).
- Image build, scan, pin by digest; atomic publish; failed builds never replace the active snapshot.

**Exit:** a build from a demo Docker host produces snapshot v1 with a tree, an image digest and a passing validation report; planted canary secrets appear nowhere in the snapshot, image, logs or LLM prompts.

### Phase 4: Gateway and stateful sessions
**Goal:** let an attacker connect and get their own twin.
- SSH gateway (prefer real OpenSSH semantics or a fingerprint-preserving proxy; support PTY **and** non-interactive `ssh host cmd`).
- Session manager: one session → one runtime → one snapshot → one overlay; session limits, idle timeout, reset = destroy + recreate.
- Synthetic services (fake web app/API and one database) driven by schemas, fixtures and deterministic state transitions.
- Event stream: every command, file access and service request recorded and linked to graph nodes.

**Exit:** two simultaneous sessions cannot see each other's changes; reconnecting after reset gives a clean twin; a session cannot reach another session or the management network.

### Phase 5: AI synthesis
**Goal:** the LLM fills gaps only, safely.
- `AIProvider` protocol with at least a local/fake provider and one remote provider.
- Context builder: minimal sanitized context, attacker content labeled untrusted, prompt-injection stripping.
- Router: real execution first → synthetic service fixture → AI only for uncovered behavior.
- Output validator: JSON schema, evidence references, resource existence, allowed state transitions, sensitive-data check. Fallback on timeout/invalid output; no unbounded retries.
- Store observation and AI proposal separately with `ai_policy_version`.

**Exit:** AI-consistency suite passes (prompt injection, invented files, inconsistent records, secret leakage); with the provider disabled the system still works using raw output and fallbacks.

### Phase 6: Dashboard and analytics
**Goal:** investigators can reconstruct what happened.
- React + TypeScript + Tailwind; WebSocket live feed (authenticated, admin only).
- Views: overview, environment tree explorer (base / visited / overlay states), session timeline, execution inspector (raw vs AI clearly labeled), environment management (build, compare, publish).
- ATT&CK tagging as a clearly separate "interpretation" layer, not stored as evidence.

**Exit:** a recorded demo session replays correctly in the UI; XSS-safe rendering of attacker-controlled output is tested.

### Phase 7: Security hardening, deployment and evaluation (Linux)
**Goal:** safe to expose, and measurable.
- Deploy twin runtime on a dedicated Linux VPS (gVisor or Firecracker/Kata where KVM exists); management plane separate; egress blocked; secrets via env/secret store.
- Escape/egress/cross-session/resource-exhaustion test suite; secret-leak canary test on the final image.
- Evaluation harness: baselines (Cowrie default, Cowrie proxy, LLM-only shell, real Ubuntu control), fingerprint probes, an LLM-attacker detection test, command fidelity and latency for AI-filled gaps.
- Short, supervised internet exposure window with data-retention policy.

**Exit:** all mandatory controls from the README pass; evaluation results exported as tables/figures for the report.

### Phase 8: Documentation and capstone deliverables
- Threat model (including what the sandbox does **not** protect against), architecture doc, user guide for Windows and Linux, related-work section using the research report, final evaluation report and demo script.

## 7. Definition of done (any task)

- Code, tests and docs updated; `lint`, `mypy` and tests pass locally and in CI (Windows + Linux).
- No new secrets, no new host-level privileges, no new egress paths.
- Security-relevant change? Add or extend an isolation or leakage test.
- README/AGENTS.md updated if behavior, commands or phases changed.

## 8. Known risks to keep visible

- Windows cannot run Firecracker/gVisor; Windows dev uses the weaker hardened-Docker driver. Never claim equal isolation.
- Real execution means real risk: treat container escape and egress abuse as design and evaluation obligations.
- Unread prior art that may affect novelty claims: TwinPot-Gen, HoneyLLM, CounterCraft (closed commercial). Re-check before final claims.
- LLM latency (1 to 4 s per call typical) and cost; cache and bound all calls.
