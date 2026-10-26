# Evaluation report

What was measured and tested for the Docker demo of Hornet v2, how, and what it shows. Every number here comes from
a file in [results/](results/) or from a test in the repository; nothing is estimated.

**Setup:** one Linux machine (CachyOS, kernel 7.2.10, x86_64), Docker 29.9.1, Python 3.14, hardened-Docker runtime,
default demo limits, two deliberately different demo servers (`shop-prod`: a shop with web app, PostgreSQL, Redis;
`helpdesk-staging`: a ticket system with API, worker, PostgreSQL, Redis). Measured 2026-10-19 to 2026-10-21.

## 1. Questions

1. Is the visitor's experience fast enough to feel like a real server?
2. Can the AI fill gaps quickly enough, and how often is its answer usable?
3. Does isolation hold: no escape, no egress, no cross-visitor leaks, no exhaustion of the host?
4. Do the observed server's secrets stay out of everything Hornet produces?
5. Does it work for more than one kind of server?

## 2. Responsiveness (question 1)

`python -m scripts.evaluate` (10 twin setups and 5 repetitions of 7 command kinds per demo server, 20 SSH round
trips each; raw samples in [results/evaluation_runtime.csv](results/evaluation_runtime.csv), summary in
[results/evaluation.md](results/evaluation.md)). Offline builds, so model latency is not mixed in.

| What | shop-prod median (p95) | helpdesk-staging median (p95) |
|---|---|---|
| One command over SSH, end to end (n = 20) | 105 ms (118) | 104 ms (119) |
| Login and first command, including creating the twin (n = 1) | 565 ms | 543 ms |
| Twin setup until ready, including the fake services (n = 10) | 433 ms (494) | 423 ms (473) |
| A fake database query from `psql` (n = 5) | 133 ms (162) | 154 ms (168) |
| A fake website request from `curl` (n = 5) | 125 ms (137) | 110 ms (122) |
| Building the environment, offline (n = 1) | 3.0 s | 3.2 s |

About 100 ms of every command is Hornet's own work after the command (filesystem diff, evidence written), on top
of the command itself. Building with an AI model (sanitization review, data model, documents) took 136 s with
`nemotron-3-super-120b-a12b` (2026-10-19), 260 to 262 s with `nemotron-3.5-super-vl-120b-a12b` (two builds,
2026-10-20) and 130 s with the same model during the demo rehearsal (2026-10-21). **Answer:** commands return in about a tenth of a second, a new visitor's copy is ready in
about half a second.

## 3. AI gap-filling (question 2)

`python -m scripts.measure_synthesis` sends 20 fixed requests the fake services cannot answer (10 PostgreSQL, 6
Redis, 4 HTTP) through the real context builder, timeout (3 s) and validator. Details in
[results/synthesis_latency.md](results/synthesis_latency.md).

| Model (hosted NVIDIA NIM, thinking off, no guided decoding) | Date | Median | p95 | Accepted |
|---|---|---|---|---|
| `nvidia/nemotron-3-super-120b-a12b` | 2026-10-19, run 1 | 1252 ms | 2033 ms | 20 / 20 |
| `nvidia/nemotron-3-super-120b-a12b` | 2026-10-19, run 2 | 973 ms | 2342 ms | 17 / 20 (3 rate-limited) |
| `nvidia/nemotron-3.5-super-vl-120b-a12b` | 2026-10-20 | 869 ms | 3001 ms | 14 / 20 (6 timeouts) |
| `nvidia/nemotron-3.5-super-vl-120b-a12b` | 2026-10-21 | 529 ms | 799 ms | 19 / 20 (1 timeout) |

Two request settings dominate latency: with the model's "thinking" on, one call took 8.9 s; with NVIDIA's guided
decoding on, 11.3 s (and one model returned HTTP 503). Pipeline overhead without a model is at most 1 ms. The
attacker never waits for any of this (answers are stored out of band). **Answer:** 0.5 to 1.3 s median; 70 to 100 % of
answers usable within 3 s, depending on the day's model availability. The first model stopped answering for this
key one day after it was measured, so the setup must check its model before a demo.

**Safety of AI output:** the AI-consistency suite (47 cases) feeds hostile and broken answers through the validator:
prompt injection, invented tables, columns and API resources, an error that contradicts the schema, claimed writes,
secret-like strings, real email domains and IP addresses, malformed JSON, provider errors and timeouts. All are
rejected or fall back as specified; no AI text appears in the execution evidence (tested end to end through the SSH
gateway). Spot checks against a live model found three validator bugs, all fixed with regression tests. Realism of
accepted answers was not measured (e.g. `EXPLAIN` once got table rows instead of a plan).

## 4. Isolation (question 3)

Tests run against real hardened-Docker twins (44 tests, none skipped, both demo servers; also in CI on GitHub's
Linux runners).

| Property | Shown by |
|---|---|
| No route out by TCP, UDP, DNS, ICMP, IPv6, HTTP or HTTPS; only the `lo` interface | `test_no_egress_by_any_protocol`, `test_no_network` |
| No path to the host, the gateway, the Docker API or cloud metadata | `test_the_twin_cannot_reach_the_host_other_sessions_or_the_gateway` |
| No Docker socket, mounts or host secrets; no capabilities; no privilege escalation; read-only root; PID 1 protected | four tests in `test_docker_isolation.py` |
| Fork bomb, memory hog, output flood and runaway commands contained; the next visitor is unaffected | `test_fork_bomb_is_contained`, `test_memory_hog_is_limited`, `test_output_flood_is_capped_and_stopped`, `test_a_fork_bomb_ends_the_connection_and_the_next_visitor_is_fine` |
| Visitors cannot see each other's files, processes or fake-service data | `test_sessions_are_isolated_from_each_other`, `test_two_simultaneous_connections_cannot_see_each_other`, `test_fake_service_state_is_not_shared_between_visitors` |
| Leaving or reconnecting leaves nothing behind | `test_reconnecting_gives_a_clean_twin_and_the_old_one_is_destroyed`, `test_destroy_and_reconcile_leave_nothing_behind` |

**Answer:** within the limits of a shared kernel, yes. A kernel exploit was not tested and is the known escape path.
Phase H added a fuller escape and abuse suite and a gVisor driver; both pass (see the threat model, section 7).

## 5. Secret leakage (question 4)

Both demo servers carry planted canary secrets (passwords, tokens, an API key, a Sentry DSN, an OIDC secret, a
person's e-mail address). Tests assert that none of them appears in: the stored profile, the build folder, any
database table (AI rows included), the audit log, the built image (a full file scan), the running twin, **any prompt
sent to a model** (sanitization review, data model, documents and run-time gap synthesis, recorded by a stand-in
model) or **any log line** (`test_full_build_publish_and_twin`, `test_canaries_reach_no_model_prompt_and_no_log`).
Credentials typed by a visitor over real SSH are stored redacted and appear nowhere in the database
(`test_credentials_an_attacker_types_are_not_stored`). Discovery made only the four allowed read calls and restarted
nothing (`test_discovery_is_read_only_and_leaks_nothing`). **Answer:** no leak found by any of these checks.

## 6. Generality (question 5)

Every build test runs the same assertions against both demo servers, which differ in project name, service names,
images, ports, environment variable names, mounts and business domain. `test_generic_shapes.py` adds 49 cases of
unusual shapes (no database, no ports, 25 mixed services, odd names). **Answer:** both servers produce consistent,
leak-free twins without any demo-specific code.

## 7. Threats to validity

- One machine, one day's conditions; small samples for builds and logins (n = 1 per server).
- Hosted AI latency and availability vary by day and account (free tier, rate limits, models disappearing).
- Two demo servers are evidence of generality, not proof; real production estates are larger.
- Isolation is shown by tests, not proven; the kernel is shared.
- Not measured: detectability by fingerprinting tools or AI attackers, behaviour of real attackers, realism of AI
  answers, concurrency beyond 5 twins, and comparison baselines (see section 9).

## 8. Related work

Prior work, as identified in the project's research notes (`reports/`, not part of this repository). The references
below were re-checked against OpenAlex/arXiv on 2026-10-21; content claims are from the notes and were not re-read.

| Reference (re-checked) | Relation to Hornet |
|---|---|
| Bridges, Mitchell, Muñoz, Henriksson. *SoK: Honeypots & LLMs, More Than the Sum of Their Parts?* arXiv 2510.25939, 2025 | Survey of the space |
| Sladić, Valeros, Catania, García. *LLM in the Shell: Generative Honeypots* (shelLM). IEEE EuroS&PW 2024 | LLM emulates the shell; Hornet runs commands for real and uses the LLM only for service gaps |
| Wang et al. *HoneyGPT: Breaking the Trilemma in Terminal Honeypots with Large Language Model*. arXiv 2406.01882, 2024 (journal version: Computer Networks, 2026) | LLM-driven terminal honeypot |
| Fan et al. *HoneyLLM: A Large Language Model-Powered Medium-Interaction Honeypot*. LNCS, 2024; and Guan, Cao, Zhu. *HoneyLLM: Enabling Shell Honeypots with Large Language Models*. IEEE CNS 2024 | Two different papers share the name; cite precisely |
| Yigit, Huseynov, Canberk. *TwinPot-Gen: A Generative Semantic Digital-Twin Honeypot Framework for Adaptive and Explainable Defence in 6G Networks*. ICC 2026 | "Digital twin" at network level, not a container-level replica |
| Tsouvalas, Nikiforakis. *Defense of the Clones: Securing Web Applications with Automatic Honeypot Generation and Deployment*. APWG eCrime 2025 | Most likely the PARALLAX work cited in the notes (venue, year and group match; the system name is not in the metadata). Clones a web application into a honeypot |
| Sladić et al. *AdvancedShelLM: A Stateful Multi-Agent LLM Honeypot for SSH Deception*. arXiv 2606.27990, 2026 | Stateful LLM shell; emulated, not executed |

Also named in the notes, not re-checked here: Cowrie (proxy and backend-pool modes run commands on real VMs, without
an LLM or a sanitized environment builder), Beelzebub, Galah, VelLMes, LLMHoney, SBASH, LLM-SSHH, Chat2Pot, HoneyPLC,
HoneyFactory, and commercial deception products (Thinkst Canary, CounterCraft, Acalvio, FortiDeceptor, Zscaler).

**Positioning.** Each ingredient has precedent. The combination appears new: read-only discovery of a Docker estate,
sanitization before any model sees data (with canary tests over every output, including prompts), a versioned
immutable synthetic environment, a disposable per-visitor twin where commands really run, an LLM used only for
validated gaps and never served as evidence, and evidence kept apart from AI output end to end. Novelty claims should
be re-checked against the full texts above before publication; search was limited and "not found" is not proof of
absence.

## 9. Not done (optional backlog)

A comparison against baselines (Cowrie default and proxy mode, an LLM-only shell, a real Ubuntu control), a
fingerprinting-script and AI-attacker detection test, and a realism rating of AI answers. These would answer "is it
convincing?", which this report does not claim.
