# Deployment guide

How to deploy Hornet as it exists today, through the hardening phase. Pick the part you need:

| Part | Use it for | Exposed to | Tested? |
|---|---|---|---|
| [1. Local demo](#1-local-demo) | Presenting and everyday use | This machine only | Yes |
| [2. Production trial on one machine](#2-production-trial-on-one-machine) | Checking the production setup safely | This machine only | Yes, rehearsed on 2026-11-05 |
| [3. Dedicated server](#3-dedicated-server) | A real honeypot | The internet (decoy SSH port only) | **Parts only; never applied to a real server** |

> **Never expose Hornet from a personal or development machine.** Internet exposure needs a dedicated server and a
> supervised window (section 3.8). Run it only against systems you are authorized to observe.

What is running: one Python process serves the admin API and the SSH gateway, and starts one disposable twin container
per visitor through Docker. PostgreSQL stores the evidence in production. The dashboard is a static website.

**Requirements:** Linux, Python 3.12 or newer, Docker, Node 20 or newer (dashboard), and gVisor (`runsc`) for production.
Windows can run the local demo with the weaker hardened-Docker driver only (see the [user guide](user-guide.md)).

`docker compose up` (`python -m scripts.dev up`) is **not** a way to deploy this. Its API container has no Docker
access by design, so it cannot start twins.

## 1. Local demo

SQLite, everything on `127.0.0.1`, settings in `.env`:

```
python -m scripts.dev setup
python -m scripts.dev ui-setup
python -m scripts.dev demo-up
python -m scripts.dev api      # terminal 1
python -m scripts.dev ui       # terminal 2: http://127.0.0.1:5173
```

Then follow [demo-commands.md](demo-commands.md). The admin key is `HORNET_ADMIN_API_KEY` in `.env`.

## 2. Production trial on one machine

This runs the real production profile (PostgreSQL, secret files, gVisor, automatic retention, unsafe settings refused)
but stays on loopback. It's the safest way to review how production behaves. Stop any running local demo first, because
it uses ports 8000 and 2222. These commands need **bash** (run `bash` first if you use fish).

**Prepare a clean copy,** without your `.env` (see section 3.4). `demo-up` in your normal checkout starts the shop
server that the trial observes.

```bash
python -m scripts.dev demo-up
git clone . ~/hornet-trial
cd ~/hornet-trial
python3 -m venv .venv
.venv/bin/pip install -q -e '.[postgres]'
```

**Start a throwaway PostgreSQL and create the secret files.** The password is for this trial only:

```bash
docker run -d --name hornet-trial-db -e POSTGRES_USER=hornet -e POSTGRES_PASSWORD=trial-only -e POSTGRES_DB=hornet \
  -p 127.0.0.1:55432:5432 postgres:16-alpine
mkdir -p data/credentials data/gateway data/builds
chmod 700 data/credentials
python3 -c "import secrets; print(secrets.token_urlsafe(32), end='')" > data/credentials/hornet_admin_api_key
printf 'postgresql+psycopg://hornet:trial-only@127.0.0.1:55432/hornet' > data/credentials/hornet_database_url
```

**Load the production settings, on loopback:**

```bash
sed -e 's#^HORNET_GATEWAY_HOST=.*#HORNET_GATEWAY_HOST=127.0.0.1#' \
    -e 's#^HORNET_GATEWAY_PORT=.*#HORNET_GATEWAY_PORT=2222#' \
    -e "s#^HORNET_GATEWAY_HOST_KEY_PATH=.*#HORNET_GATEWAY_HOST_KEY_PATH=$PWD/data/gateway/host_key#" \
    -e "s#^HORNET_BUILD_DIR=.*#HORNET_BUILD_DIR=$PWD/data/builds#" \
    -e 's#^HORNET_DISCOVERY_ALLOWED_PROJECTS=.*#HORNET_DISCOVERY_ALLOWED_PROJECTS=shop-prod#' \
    deployment/hornet.env.example > data/trial.env
set -a; . data/trial.env; set +a
export CREDENTIALS_DIRECTORY=$PWD/data/credentials
.venv/bin/python -m scripts.dev safety
```

`safety` should show `profile prod`, `gVisor`, `length + hash only` for passwords and `30 days (automatic, hourly)`.

**Migrate and start (leave this terminal running):**

```bash
.venv/bin/python -m scripts.dev migrate
.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --no-server-header
```

**In a second terminal, build and publish a twin:**

```bash
cd ~/hornet-trial
KEY=$(cat data/credentials/hornet_admin_api_key)
API=http://127.0.0.1:8000/api/v1
curl -s $API/health; echo
curl -s -o /dev/null -w 'without the key: %{http_code}\n' $API/environments
ID=$(curl -s -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"source":"docker","project":"shop-prod"}' $API/environments/build \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s -X POST -H "X-API-Key: $KEY" $API/environments/$ID/validate > /dev/null
curl -s -X POST -H "X-API-Key: $KEY" $API/environments/$ID/publish | cut -c1-100; echo
```

Expected: health shows `"database":"up"` and `"gateway":"listening:2222"`, the request without the key gets `401`, and
the last line shows `"status":"published"`.

**Attack it,** with the password `123456`:

```bash
ssh -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@127.0.0.1 uname -r
```

It prints `4.4.0`, gVisor's kernel.

**Try to tamper with the evidence.** The database itself refuses both changes, with errors like
`hornet: session_event is append-only (UPDATE refused)`:

```bash
docker exec hornet-trial-db psql -U hornet hornet -c "update session_event set event_type='x'"
docker exec hornet-trial-db psql -U hornet hornet -c "delete from admin_audit_event"
```

**Clean up:** press Ctrl-C in the first terminal, then:

```bash
docker rm -f hornet-trial-db
docker images hornet-twin --format '{{.Repository}}:{{.Tag}}  {{.CreatedSince}}'    # remove only the trial's image
rm -rf ~/hornet-trial
```

## 3. Dedicated server

**Status:** the settings, secret files, PostgreSQL guards, gVisor twins, image transfer and the dashboard proxy
configuration were tested on one machine. The firewall, the systemd service and the WireGuard layout have **not** been
applied to a real server. Expect to fix small details on your first install.

```
internet ── TCP 22 (decoy) ──> HONEYPOT HOST: Hornet + Docker + gVisor
                                    │ WireGuard
                               MANAGEMENT HOST: PostgreSQL + dashboard ──WireGuard── investigators

BUILD MACHINE: can see the real server's Docker; builds twins, never receives attackers
```

The examples use `10.66.0.1` (honeypot) and `10.66.0.2` (management). [`deployment/README.md`](../deployment/README.md)
explains the design and the data retention policy.

### 3.1 Honeypot host: base system

Use a dedicated, updated Linux VM or VPS with nothing else on it. Set up WireGuard (`wg0`) first, and move your real
sshd to the VPN **before** enabling the firewall, or you will lock yourself out. In `/etc/ssh/sshd_config`:

```
Port 2202
ListenAddress 10.66.0.1
```

Run `sudo systemctl restart sshd`, then check in a **new** session that you can still log in on port 2202.

### 3.2 Docker and gVisor

Install Docker Engine and gVisor ([official instructions](https://gvisor.dev/docs/user_guide/install/)), then merge
`deployment/docker-daemon.json` into `/etc/docker/daemon.json`. Its `path` must match where `runsc` is installed:

```bash
which runsc
sudo systemctl restart docker
docker run --rm --runtime=runsc hello-world
```

### 3.3 Install Hornet

```bash
sudo useradd --system --create-home --home-dir /opt/hornet-home --groups docker hornet
sudo git clone <your repository URL> /opt/hornet
cd /opt/hornet
sudo python3 -m venv .venv
sudo .venv/bin/pip install -e '.[postgres]'
sudo docker build -t hornet-twin-base:2 twin-image
sudo mkdir -p data/gateway data/builds
sudo chown -R hornet:hornet data
sudo chmod 700 data
```

Being in the `docker` group is equivalent to root on this host, which is why it must be a dedicated machine.

### 3.4 Secrets

> **Do not run `python -m scripts.dev setup` here, and do not keep a `.env` in `/opt/hornet`.** Settings are read in this
> order: environment variables, secret files, then `.env`. Secret files beat a `.env`, but its other values would still
> apply. Check: `test ! -e /opt/hornet/.env && echo ok`

```bash
sudo mkdir -p /etc/hornet/credentials
sudo chmod 700 /etc/hornet/credentials
python3 -c "import secrets; print(secrets.token_urlsafe(32), end='')" | sudo tee /etc/hornet/credentials/admin_api_key > /dev/null
printf 'postgresql+psycopg://hornet:<password>@10.66.0.2:5432/hornet?sslmode=require' | sudo tee /etc/hornet/credentials/database_url > /dev/null
sudo chmod 600 /etc/hornet/credentials/*
```

`hornet.service` also loads `nvidia_api_key`, and systemd won't start the service if a listed file is missing. Without
AI, delete that `LoadCredential` line from your copy of the service. With AI, put the key in that file.

### 3.5 Management host

**PostgreSQL 16**, listening on the VPN only, with TLS. In `postgresql.conf` set `listen_addresses = '10.66.0.2'` and
`ssl = on`. In `pg_hba.conf`:

```
hostssl hornet hornet 10.66.0.1/32 scram-sha-256
```

Use an ordinary role, not a superuser: a superuser can switch off the evidence guards.

```bash
sudo -u postgres createuser --pwprompt hornet
sudo -u postgres createdb --owner hornet hornet
```

**Dashboard.** Build it anywhere and copy the result:

```bash
cd frontend && npm ci && npm run build
rsync -a dist/ <management host>:/var/www/hornet/
```

Serve it with TLS, for example this nginx site. Tested with a self-signed certificate: pages, API and the live feed
work, and a foreign origin is refused.

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ""      close;
}

server {
    listen 443 ssl;
    server_name hornet.vpn.internal;
    ssl_certificate     /etc/hornet/tls/fullchain.pem;
    ssl_certificate_key /etc/hornet/tls/privkey.pem;
    allow 10.66.0.0/24;
    deny all;
    root /var/www/hornet;
    location / {
        try_files $uri /index.html;
    }
    location /api/ {
        proxy_pass http://10.66.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 600s;
    }
}
```

`HORNET_DASHBOARD_ORIGINS` must be exactly this site's address (section 3.6).

### 3.6 Configure and start the honeypot host

```bash
sudo cp deployment/hornet.env.example /etc/hornet/hornet.env
sudoedit /etc/hornet/hornet.env        # set HORNET_DASHBOARD_ORIGINS; review the limits
sudo cp deployment/hornet.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Edit `PUBLIC_IF` and the other values at the top of `deployment/nftables.conf`, then check and apply it:

```bash
sudo nft -c -f deployment/nftables.conf
sudo cp deployment/nftables.conf /etc/nftables.conf
sudo systemctl enable --now nftables
```

The ruleset begins with `flush ruleset`. Applying it removes other firewall rules on the host, including Docker's. Twins
use no network, so they don't need them.

```bash
sudo systemctl enable --now hornet
systemctl status hornet
journalctl -u hornet -n 50
```

The service runs `safety` and the migrations before starting, and refuses to start on unsafe settings. On a first
install, migrations also add the evidence guards to PostgreSQL.

### 3.7 Build and publish a twin

Do this on the **build machine**, which can see the real server's Docker. Set it up like section 2, with:

- `HORNET_DATABASE_URL` or the secret file pointing at the management database over the VPN.
- `HORNET_DISCOVERY_ALLOWED_PROJECTS` set to the **one** project you are authorized to observe.
- `HORNET_GATEWAY_ENABLED=false`, because this machine must not accept attackers.

Start the API on loopback, then build and validate as in section 2, **without publishing yet**. Copy the image to the
honeypot host, keeping its ID:

```bash
docker images hornet-twin --format '{{.ID}}  {{.CreatedSince}}'      # the newest is yours
docker save <ID> | ssh -p 2202 <admin>@10.66.0.1 docker load
```

Check on the honeypot host that `docker image inspect <ID>` works. Only then publish:

```bash
curl -s -X POST -H "X-API-Key: $KEY" $API/environments/$ID/publish
```

New visitors now get this version. Publishing archives the previous version; a failed build never replaces it.

### 3.8 Before any internet exposure

Run this on the honeypot host, with the service stopped:

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest backend/tests/test_docker_hardening.py backend/tests/test_postgres.py -rs
```

The tests use throwaway containers. Only the skips starting with `accepted:` are acceptable. Then:

1. **From another network,** run `nmap -p- <public IP>`: only port 22 (and UDP 51820, which needs `-sU` to see) should
   answer. The API and PostgreSQL must not.
2. **Check `/etc/hornet/hornet.env`:** `HORNET_STORE_RAW_COMMANDS=false` and `HORNET_GATEWAY_LOG_PASSWORDS=false`.
3. **Rotate** the admin and NVIDIA keys.
4. **Agree** a short window (24 to 72 hours) and who watches the dashboard.
5. **Check your organisation's rules.** Recorded data can include attacker IP addresses and third parties' credentials.

To stop: `sudo systemctl stop hornet`, which destroys all twins. Afterwards, export what you need and rotate the keys
again. Retention deletes visits after 30 days.

## Troubleshooting

| Problem | Cause and fix |
|---|---|
| `SQLite is not allowed when HORNET_ENV=prod` | The database secret file is missing or misnamed, or `HORNET_DATABASE_URL` is set in the environment. Check the credential names |
| `at least 32 characters` / `must be https://` / `automatic retention` | The production rules: use a longer key, an https dashboard address and `HORNET_RETENTION_AUTO=true` |
| The service fails with `status=243/CREDENTIALS` | A file listed by `LoadCredential` is missing (section 3.4) |
| Twins won't start | Docker is down, the `hornet` user isn't in the `docker` group, or `runsc` isn't registered: `docker run --rm --runtime=runsc hello-world` |
| SSH connects, then closes | No version is published, or its image isn't on this host (section 3.7) |
| Dashboard loads but "Live" never connects | `HORNET_DASHBOARD_ORIGINS` doesn't match the address in the browser, or the proxy's `Upgrade` and `Connection` lines are missing |
| Locked out after enabling the firewall | Use your provider's console. The real sshd must already be on the VPN (section 3.1) |
