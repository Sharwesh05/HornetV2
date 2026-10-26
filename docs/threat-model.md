# Threat model

Scope: Hornet v2 as built for the demo (hardened-Docker runtime, demo profile `HORNET_ENV=dev`, everything on
loopback, one Linux machine), plus the Phase H additions: a gVisor runtime, the `prod` profile, database-level guards
and a deployment design (`deployment/`). Each threat lists its mitigation, the evidence that the mitigation works (a
test or a measurement), and what remains. *Updated 2026-10-26 from the Phase H results (section 7); Phase H awaits
approval, and no exposure happens before it.*

## 1. What we protect

| Asset | Why it matters |
|---|---|
| **The host** (the machine running Docker and Hornet) | A twin escape or an API compromise would give the attacker this machine |
| **The observed real server** and its secrets | Hornet reads it; its secrets must never reach the decoy, a model, a log or the database |
| **The admin API key** and Hornet's own configuration (NVIDIA key, database) | Whoever holds the admin key controls Hornet, which controls Docker |
| **Recorded evidence** | Its integrity is the point of the system; it may contain third parties' credentials typed by attackers |
| **Other visitors** | One visitor must not see or affect another's twin |
| **Investigators** viewing the dashboard | Attacker-controlled text must not run in their browser |

## 2. Who we defend against

| Actor | Capability assumed |
|---|---|
| **Visitor (attacker)** | Full control of everything they type over SSH: commands, file names, requests to the fake services, terminal escape sequences, HTML/JS payloads, prompt-injection text |
| **The AI model / provider** | Semi-trusted: may be wrong, unavailable, slow, manipulated by injected text, or return hostile output; sees what we send |
| **A malicious page in the investigator's browser** | Can try to reach the local API or live feed cross-origin |
| **Operator mistakes** | Unsafe settings, a wrong allowlist, a real secret in a config file |

Out of scope: a malicious administrator with the admin key, a compromised host, supply-chain attacks on dependencies
and base images (pinned by version and lockfile, not audited), physical access.

## 3. Trust boundaries

```
 visitor ──ssh──▶ [B1 gateway] ──▶ [B2 twin container │ kernel shared with host]
                                         │ fake services inside the twin (B3)
 Hornet API/orchestrator ──Docker socket (root-equivalent)──▶ Docker daemon (B4)
 Hornet ──4 read calls──▶ observed server's Docker metadata (B5)
 Hornet ──redacted metadata / untrusted JSON──▶◀── AI provider (B6)
 investigator browser ──X-API-Key over loopback──▶ admin API + live feed (B7)
 database (evidence, audit) (B8)
```

## 4. Threats, mitigations and evidence

| # | Threat | Mitigation | Evidence | Residual risk |
|---|---|---|---|---|
| T1 | Visitor escapes the twin to the host (B2) | No capabilities, `no-new-privileges`, default seccomp, read-only root, no setuid binaries, non-root user, no mounts, no Docker socket, PID 1 owned by another user | `test_docker_isolation.py`; `test_docker_hardening.py` on **runc and gVisor**: capabilities, `no-new-privileges`, seccomp, setuid search, socket, host paths, devices, `/proc/sys` and `/sys` writes, `/proc/kcore`, mount, namespaces, mknod, raw/packet sockets, chroot, PID 1 | **runc shares the host kernel:** a kernel exploit escapes. With `HORNET_RUNTIME_DRIVER=gvisor` the visitor's system calls go to gVisor's own kernel; the exceptions accepted under gVisor are in section 5 |
| T2 | Visitor uses the twin to attack others (egress) | `network_mode=none`; fake services on loopback aliases | `test_no_egress_by_any_protocol` (TCP, UDP, DNS, ICMP, IPv6, HTTP/S, no route); gateway probes to host, Docker API, cloud metadata | None found on runc or gVisor (`test_no_egress`, 7 probes per runtime). On a server the host firewall also forwards nothing (`deployment/nftables.conf`) |
| T3 | Visitor exhausts the host (fork bomb, memory, disk, output, many sessions) | cgroup limits (memory, CPU, pids, nofile), tmpfs size, per-command timeout and output cap, session cap and TTLs, per-IP connection cap; demo profile refuses looser limits | Fork bomb, memory hog (pages actually touched), output flood, long loops, tmpfs fill on runc and gVisor; 5 parallel SSH visitors with the 6th refused; idle/TTL reaper and restart recovery on real Docker (`test_docker_hardening.py`); `test_demo_safety.py` | Limits per twin; total = 5 twins at most. Under gVisor the pids limit counts gVisor's own threads (64 extra are added) and a fork bomb ends the whole twin rather than filling it; both end only that visitor. Not load-tested beyond 6 connections |
| T4 | One visitor reads or changes another's twin | One container per connection; per-twin in-memory fake-service state; separate session records | Cross-session files and processes (`test_sessions_are_isolated_from_each_other`), simultaneous gateway sessions, fake-service state (`test_fake_service_state_is_not_shared_between_visitors`) | Shared kernel (T1) |
| T5 | Visitor reaches Hornet itself (gateway, API, Docker) | No network; the twin cannot resolve or route to the host | Gateway test probes `127.0.0.1:<gateway>`, `host.docker.internal`, `172.17.0.1:2375`, `169.254.169.254` | Shared kernel (T1) |
| T6 | **API compromise = host compromise** (Hornet holds the Docker socket, B4) | Admin key (16+ chars, placeholders refused, constant-time check); the gateway and dashboard origins must be loopback in the demo profile (startup check); the API is started on `127.0.0.1` by `scripts/dev.py` and the compose file (not enforced by a check); attacker data never reaches the API except as stored data | `test_api.py` auth tests, `test_demo_safety.py` | The API process is root-equivalent on the host. `deployment/` puts it on a dedicated host with the admin API on the VPN address only and PostgreSQL on a separate management host, but the API and the gateway are still **one process** (see `deployment/README.md`) |
| T7 | Real secrets from the observed server leak into the decoy, a model prompt, logs or the database (B5, B6) | Env values dropped at discovery; regex redaction; AI review can only remove; `assert_clean` last; builder leak scan; prompts get only redacted text | Canary tests on both demo servers: profile, build folder, database (AI rows included), audit log, image, running twin, **every model prompt and every log line** (`test_canaries_reach_no_model_prompt_and_no_log`) | Regexes are heuristic; names and internal hostnames depend on the AI review. Keep the allowlist narrow and preview before building |
| T8 | Discovery changes or harms the observed server | `ReadOnlyDocker` exposes four read calls and nothing else; allowlist; Hornet's own twins excluded | `test_discovery_is_read_only_and_leaks_nothing` records every API call and checks nothing restarted | Reads still need the Docker socket on that host |
| T9 | Hostile or wrong AI output becomes "evidence" or reaches the visitor (B6) | Out of band (5A): never served; stored only in `ai_synthesis`, never in `execution_observation`; validated in code (shape, schema references, no state change, no secrets/real emails/IPs, size) | `ai_consistency/` (47 cases: injection, invented tables/columns/resources, contradictions, writes, secrets, malformed, timeout), `test_synthesis.py`, separation tests in the dashboard | Validation checks safety and consistency, not realism (see limitations) |
| T10 | Prompt injection through attacker text | Attacker text is labelled untrusted, capped, stripped of common injection phrasing and control characters; the system policy forbids following it; output validated regardless | Injection case in `ai_consistency/` | Stripping is best effort; the defence is validation (T9) |
| T11 | XSS in the investigator's browser (B7) | Attacker text rendered only as React text nodes; ESLint forbids every HTML-injection API; control/bidi characters shown visibly; no attacker text in attributes | `xss.test.tsx` (8 payloads through every component; shown to fail when HTML rendering is introduced) | Browser bugs |
| T12 | A hostile web page reaches the local API or live feed (B7) | CORS allows only the dashboard's loopback origins (no wildcard, no cookies); the live feed refuses foreign `Origin` (4403) and needs the key in its first message (4401); the key is never in URLs or storage | `test_dashboard_api.py`: CORS, origin refusal, key refusal | Anything that can read the key from the operator's machine |
| T13 | Evidence is altered or deleted | Append-only and immutability guards on every ORM flush; published snapshots frozen; one audited purge path | `test_immutability.py`, `test_command_storage.py`; on PostgreSQL 16, migration 0005's triggers refuse raw-SQL updates/deletes of published snapshots, their nodes and images and every append-only table; only the purge's own transaction may delete, never the audit log (`test_postgres.py`) | A database superuser can disable triggers; keep the application role a non-superuser on the server |
| T14 | Third-party credentials typed by attackers end up stored | Commands stored with credentials redacted; raw text off (refused in the demo profile); login passwords as length + hash by default; reads audited; 30-day retention purge | `test_command_storage.py`, real-SSH test `test_credentials_an_attacker_types_are_not_stored` | stdout/stderr are kept as evidence and may echo a secret. In `prod` the purge runs hourly (`HORNET_RETENTION_AUTO`, required there) |
| T15 | Unsafe configuration by the operator | Validated settings; unsafe values fail startup; the demo profile may only tighten; `python -m scripts.dev safety` shows the effective settings | `test_demo_safety.py`, `test_api.py`, `test_prod_profile.py` (prod refuses short keys, non-https dashboard origins, SQLite, advisory AI review, manual retention; secrets from secret files), `test_deployment_files.py` | Settings cannot check the firewall or network layout; that is the deployment checklist |

## 5. What hardened Docker does **not** protect against

- **Kernel vulnerabilities (runc).** With the default driver every twin shares the host kernel, and a kernel exploit
  from inside a twin defeats all container controls. On Windows the "host" is Docker Desktop's VM and only this
  weaker driver exists. The gVisor driver (Linux) narrows this to gVisor's own kernel and its small host interface;
  it is not a VM, and a gVisor bug is the remaining escape path.
- **Accepted differences under gVisor** (each skipped test names its reason, starting with "accepted:"):
  `unshare -Ur` and `mknod` succeed, but inside gVisor: in the new namespace mount, `/proc/sys` writes and `/dev/mem`
  are still refused, and the device node cannot be opened (`test_gvisor_namespaces_and_devices_stay_inside_the_sandbox`).
  The host seccomp check does not apply (gVisor filters its own host calls). `/proc` does not show `CapAmb` or
  `NoNewPrivs`; `no-new-privileges` is checked through the Docker API instead.
- **Detection.** A careful visitor can tell it is a honeypot (host kernel string, `/.dockerenv`, the fake-service
  process, the command wrapper in `ps`, no network). This leaks that the target is a decoy, not data. See
  [limitations.md](limitations.md).
- **Resource contention at scale.** Limits are per twin and the session cap is tested (5 parallel visitors, the 6th
  refused); behaviour with many hosts' worth of visitors is not measured.
- **Side channels** (timing, cache) between twins on one host.

## 6. Command and credential data

| What | Stored as | Who can read it |
|---|---|---|
| Typed commands | credentials redacted (`input_text`), plus a SHA-256 of the raw input | Admin API only; each read audited |
| Raw commands | not stored (refused in the demo profile; possible in `prod` with a warning) | n/a |
| Login passwords | length + short hash by default; text only if `HORNET_GATEWAY_LOG_PASSWORDS=true` | Admin API |
| Command output | as produced (evidence) | Admin API only |
| Anything sent to a model | redacted, minimal context; never fake or real secrets | the AI provider |

Retention: `python -m scripts.dev purge` deletes ended sessions older than `HORNET_RETENTION_DAYS` (30) with
everything recorded for them, and audits the purge.

## 7. Phase H results (2026-10-26, awaiting approval)

Measured on Linux (CachyOS, kernel 7.3, Docker with runc 1.5.2 and gVisor `runsc` from the Arch User Repository):

| Item | Result |
|---|---|
| H1 concurrency, restart, reaper | 5 parallel SSH visitors isolated, 6th refused, slot reused; orphaned twins removed and stranded sessions failed after a restart; idle and TTL reaping on real Docker |
| H2 PostgreSQL | migrations 0001 to 0005 up, down and up on PostgreSQL 16; 9 triggers; raw-SQL tampering refused |
| H3 admin console and `prod` | `session.exec` / `session.exec_refused` audited (no command text); prod profile rules tested; automatic retention |
| H4 escape and abuse suite | passes on runc and gVisor (`test_docker_hardening.py`), accepted gVisor differences in section 5 |
| H5 mypy | clean, strict on sanitization, AI and orchestration; runs in `lint` on both CI systems |
| H6 gVisor | `HORNET_RUNTIME_DRIVER=gvisor`; the whole real-Docker suite (isolation, gateway, build, canaries) also passes with every twin under gVisor (`HORNET_TEST_RUNTIME=gvisor`) |
| H7 deployment | `deployment/` (firewall, Docker daemon config, systemd unit, env example, README with retention policy); file checks in `test_deployment_files.py`; not applied to a real server |
| H8 canaries | the canary tests pass on the final images under both runtimes |

Not done by Phase H: applying `deployment/` to a real server, splitting the gateway from the admin API, load testing.
**No public exposure happens before Phase H is approved,** and then only per `deployment/README.md` section 6.
