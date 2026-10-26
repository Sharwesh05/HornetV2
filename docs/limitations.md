# Known limitations of the Docker demo (Phase 7.4)

What Hornet does **not** do, or does only partly, in its current hardened-Docker form. Each row says how we know (the
probe or test that showed it) and where it is handled. "Verified" rows were observed on a live twin on 2026-10-21
(Linux 7.2.10, Docker 29.9.1, shop-prod environment). Nothing here is fixed in the demo; stealth work is out of scope
(see the plan), and security items are in the mandatory post-presentation hardening phase (H).

## 1. A careful visitor can tell it is a honeypot

Automated bots rarely check these; a human or an AI-driven attacker doing basic reconnaissance can.

| Tell | What the visitor sees | How we know | Handled in |
|---|---|---|---|
| Host kernel | `uname -r` prints the host's kernel (`7.2.10-1-cachyos` here; a `...-WSL2` kernel on Windows), not Debian's | verified | under gVisor it prints `4.4.0` instead: a different tell, not a fix |
| Container marker | `/.dockerenv` exists (also under gVisor) | verified | out of scope (stealth) |
| Process list | `ps` shows `docker-init`, `sleep infinity`, the fake-service program `python3 /usr/share/hornet/emulator.py`, and **the wrapper around each command** (`timeout -s KILL 5.000 /bin/bash -c cd -- /home/user ...` with Hornet's state marker); no real `postgres`, `nginx` or `sshd` | verified | out of scope (stealth) |
| Readable emulator | `/usr/share/hornet/emulator.py` is world-readable and names itself | verified | out of scope (stealth) |
| No network | only `lo` exists; no route out; service names resolve to `127.0.1.x` | verified (and `test_no_egress_by_any_protocol`) | by design (safety) |
| Hard 5 s limit | `sleep 7` is killed (exit 137); downloads, builds and long scans fail | verified | by design; limits are server policy |
| Missing admin tools | `sudo`, `docker`, `systemctl` are absent, although the planted `.bash_history` mentions `docker` | verified | out of scope (fake commands not wanted) |
| Login name | whatever name is used to log in, `whoami` is `user` | verified | out of scope |
| No terminal | `tty` says "not a tty"; no `vim` (only `nano`), no `top`, no password prompts, no job control; Ctrl-C does not interrupt a running command | verified (`tty`, `command -v`) and design | out of scope |
| Uptime | `/proc/uptime` is the host's uptime under runc; under gVisor it starts at 0 when the visitor's twin starts | verified | out of scope (stealth) |
| gVisor boot log | under gVisor, `dmesg` prints "Starting gVisor..." | verified | out of scope (stealth) |
| SSH handshake | the banner says OpenSSH, but the key exchange is asyncssh's; one ed25519 host key; any password works on the 3rd try | design (code) | out of scope |
| cgroup | `/proc/1/cgroup` shows only `0::/` (private cgroup namespace): **not** a tell | verified | n/a |

## 2. Isolation

| Limitation | Detail | Handled in |
|---|---|---|
| Shared kernel | Hardened Docker (no capabilities, read-only root, seccomp, no network, limits) still shares the host kernel; a kernel exploit is the escape path | `HORNET_RUNTIME_DRIVER=gvisor` on Linux (the escape suite passes on both; a gVisor bug is then the path) |
| Windows is weaker | On Windows the twin runs in Docker Desktop's VM; never claim equal isolation | documented (AGENTS.md) |
| Tested, not proven | Isolation is shown by tests (no egress by TCP/UDP/DNS/ICMP/IPv6/HTTP, no Docker socket, no capabilities, fork bomb, memory, output flood, cross-session files, processes and fake-service state), not by a formal argument | the Phase H suite (`test_docker_hardening.py`) covers more probes, on runc and gVisor |

## 3. Fake services and AI

| Limitation | Detail | Handled in |
|---|---|---|
| Fake PostgreSQL is a subset | Some valid SQL (`UNION`, `string_agg`, window functions, `LATERAL`) gets "column does not exist" (42703) and is **not** flagged as a gap, so no AI suggestion is made for it | open decision (emulator fix) |
| Other protocols | PostgreSQL extended protocol (prepared statements) is refused; MySQL exists as files only; the HTTP API is a simple table API | backlog |
| AI is out of band | The visitor always gets the fake service's error; AI suggestions are stored for investigators only (Phase 5A); live serving (5B) is not built | optional backlog |
| AI realism is not measured | The validator checks safety and consistency, not realism (e.g. `EXPLAIN` once got table rows instead of a plan) | evaluation backlog |
| Hosted model availability | Hosted NVIDIA models change: the default model returned 404 one day after it was measured; with `HORNET_AI_SANITIZER_MODE=required` a build then fails closed | operational; pin a working model in `.env` |

## 4. Data handling

| Limitation | Detail | Handled in |
|---|---|---|
| Output is stored as-is | stdout/stderr are evidence and are not redacted: a command that echoes a secret back keeps it | documented (README, *Command storage*) |
| Password logging is configurable | The default stores only length and hash; a `.env` can turn text logging on | `python -m scripts.dev safety` shows it |
| Retention is manual | `python -m scripts.dev purge` is not scheduled | H3 |

## 5. Operations

| Limitation | Detail | Handled in |
|---|---|---|
| Single process | Live twins are tracked in memory; restarting the API destroys every twin; a visit cannot be resumed | H1 (restart recovery tests) |
| SQLite in the demo | Fine for one machine; production requires PostgreSQL (enforced at startup) | H2 |
| Baselines not measured | No comparison yet against Cowrie, an LLM-only shell or a real host | optional backlog (7.7) |
