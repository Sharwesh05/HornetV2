# Deploying Hornet beyond one machine (Phase H7)

**Status: reviewed design, not yet deployed.** These files are checked (see *Verification*) but have not been applied
to a real host. Do not expose Hornet to the internet until Phase H is approved, and then only for a supervised window
(section 6).

## 1. Layout

```
                 internet
                    │  TCP 22 (decoy SSH) only
        ┌───────────▼─────────────────────────────────────┐
        │ HONEYPOT HOST (dedicated Linux VM/VPS, KVM)      │
        │  nftables: drop everything else, forward nothing │
        │  Docker + gVisor: one twin per visitor (runsc,   │
        │    network none, limits)                         │
        │  hornet.service: SSH gateway (public :22) +      │
        │    admin API (VPN address :8000 only)            │
        │  real sshd for operators: :2202, VPN only        │
        └───────────┬─────────────────────────────────────┘
                    │ WireGuard (wg0) only
        ┌───────────▼─────────────────────────────────────┐
        │ MANAGEMENT HOST                                   │
        │  PostgreSQL 16 (listens on the VPN address only)  │
        │  dashboard (static build) behind TLS, VPN only    │
        └───────────┬─────────────────────────────────────┘
                    │ WireGuard
              investigators' laptops
```

- **Only the decoy is public.** The admin API, the dashboard, PostgreSQL and the operators' real SSH are reachable
  only over WireGuard.
- **Twins use gVisor** (`HORNET_RUNTIME_DRIVER=gvisor`): each visitor's system calls go to gVisor's user-space kernel,
  not the host kernel. The hardened-Docker controls (no network, no capabilities, limits, read-only root) still apply.
- **The host forwards nothing** (`deployment/nftables.conf`): even a misconfigured container cannot reach the
  internet, the LAN or the management host.
- **Discovery of the real server is not done from here.** Build environments on a machine that may observe the real
  Docker setup, publish to the shared database, and let the honeypot host only *run* published snapshots (its
  `HORNET_DISCOVERY_ALLOWED_PROJECTS` stays empty). The built twin image must then be copied to the honeypot host
  (`docker save | ssh ... docker load`); the snapshot pins it by image ID.

**Known limitation.** In the current code the admin API and the SSH gateway run in **one process**, which holds
Docker access (root-equivalent on that host). The mitigations above (admin API only on the VPN address, a dedicated
host, gVisor, a forward-nothing firewall) reduce but do not remove this: a remote exploit in the gateway process
would expose the admin side on that host. Splitting the gateway and orchestrator from the admin API is the next
structural step if Hornet is run for longer than a supervised window.

## 2. Files

| File | Goes to | Purpose |
|---|---|---|
| `nftables.conf` | `/etc/nftables.conf` | Default-drop firewall: public decoy port and WireGuard in; nothing forwarded; narrow output |
| `docker-daemon.json` | `/etc/docker/daemon.json` (merge) | Registers `runsc`; no inter-container traffic; no new privileges by default; log limits |
| `hornet.service` | `/etc/systemd/system/` | Runs Hornet as an unprivileged user with systemd sandboxing; secrets as credentials; runs the safety check and migrations before start |
| `hornet.env.example` | `/etc/hornet/hornet.env` | Non-secret production settings (`HORNET_ENV=prod`, gVisor, retention) |

## 3. Install on the honeypot host (outline)

1. A dedicated VM/VPS (Debian/Ubuntu or similar, KVM), fully updated, nothing else running. Move the operators' real
   sshd to port 2202 and restrict it to the VPN (`ListenAddress 10.66.0.1`), *before* enabling the firewall.
2. WireGuard to the management host and to investigators (`wg0`, e.g. `10.66.0.0/24`).
3. Docker Engine, then gVisor from the official release (see the user guide), then merge `docker-daemon.json` and
   restart Docker. Check: `docker run --rm --runtime=runsc hello-world`.
4. A system user `hornet` (member of `docker`), the code in `/opt/hornet`, `python -m scripts.dev setup`, the twin
   base image (`docker build -t hornet-twin-base:2 twin-image`), and the published snapshot's image (`docker load`).
5. Secrets as files, mode 0600, owner root: `/etc/hornet/credentials/{admin_api_key,database_url,nvidia_api_key}`
   (the admin key at least 32 characters; the database URL points at the management host over the VPN, with TLS).
6. `/etc/hornet/hornet.env` from `hornet.env.example`; `sudo nft -c -f deployment/nftables.conf`, then apply it.
7. `systemctl enable --now hornet`. The service refuses to start if a setting is unsafe (`scripts.dev safety`).

On the management host: PostgreSQL 16 listening on the VPN address only (`listen_addresses = '10.66.0.2'`,
`hostssl` entries for the honeypot host), `alembic upgrade head` (migration 0005 adds the database-level guards),
and the dashboard as a static build (`npm run build`) behind a TLS reverse proxy on the VPN, whose origin is set in
`HORNET_DASHBOARD_ORIGINS`.

## 4. Secrets

- Never in `.env`, images, logs or the repository. On a server: systemd credentials (this setup), or Docker/Kubernetes
  secrets mounted at `/run/secrets`. Hornet reads a secret file named like the setting (e.g. `hornet_admin_api_key`)
  when the environment variable is not set; `HORNET_SECRETS_DIR` overrides the directory.
- Rotate the admin key and the NVIDIA key before the exposure window and after it.

## 5. Data retention policy

| Data | Kept | How it is removed |
|---|---|---|
| Visits (commands redacted, output, events, file changes, AI suggestions) | 30 days after the visit ends | `HORNET_RETENTION_AUTO=true`: hourly audited purge (required in prod) |
| Login attempts that never became a visit | 30 days | same purge |
| Administrator audit log | kept (append-only, never purged) | manual decision, outside Hornet |
| Raw (unredacted) commands | not stored (`HORNET_STORE_RAW_COMMANDS=false`) | n/a |
| Attempted passwords | length + hash only (`HORNET_GATEWAY_LOG_PASSWORDS=false`) | with the visit |
| Database backups | operator's choice; encrypt them and expire them within the same 30 days | outside Hornet |

Recorded data can contain third parties' credentials typed by attackers and attackers' IP addresses (personal data in
many jurisdictions). Check your organisation's rules before collecting it.

## 6. Before and during an exposure window

1. Phase H approved (all of `docs/threat-model.md` section 7 done); CI green.
2. On the honeypot host: `python -m scripts.dev test -rs`, then `HORNET_TEST_RUNTIME=gvisor python -m scripts.dev test -rs -m docker`: the escape,
   egress, cross-visitor and exhaustion suite passes on gVisor.
3. From outside: only TCP 22 answers (`nmap` from another network); the admin API and PostgreSQL do not.
4. Supervised: someone watches the dashboard during the window; a short, defined window (e.g. 24 to 72 hours);
   stop with `systemctl stop hornet` (destroys all twins).
5. Afterwards: export what you need, rotate keys, let retention run or purge.

## 7. Verification of these files

- `nftables.conf`: the full ruleset passes `nft -c` in an isolated network namespace except the connection-tracking
  (`ct`) rules, which need root to check: run `sudo nft -c -f deployment/nftables.conf` on the target host.
- `hornet.service`: passes `systemd-analyze verify` (with the paths pointed at a checkout).
- `docker-daemon.json`: valid JSON; its `runsc` entry matches what `runsc install` writes.
- `hornet.env.example`: a test loads it with fake secrets and checks that the production profile accepts it
  (`backend/tests/test_deployment_files.py`).
