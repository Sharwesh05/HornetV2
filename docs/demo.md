# Demo runbook (current state: Phase 6)

This walks through what Hornet does **today**: observe a running Docker server, build a sanitized twin, let an
"attacker" SSH into a disposable copy of it, and read back everything they did. Everything runs on your machine and
binds to `127.0.0.1` only. Do not expose the gateway to a network (AGENTS.md rule 10).

AI synthesis is **out of band** (Phase 5A): when a fake service cannot answer a request, the attacker gets its
controlled error; afterwards the system stores a validated AI proposal for that gap, labelled "AI proposal, not
served to attacker". Nothing AI-generated is ever shown to the attacker. Results can be read through the admin API
(step 5) or the dashboard (step 5b).

Commands are for Linux/macOS. On Windows use the same `python -m scripts.dev ...` commands in PowerShell.

## 1. One-time setup

Needs Python 3.12+ and a running Docker (Linux: `sudo systemctl start docker`).

```bash
python -m scripts.dev setup
```

This creates `.venv`, installs the project, and creates `.env` with a random admin key (an existing `.env` is left
alone). Then edit `.env` and set:

```bash
HORNET_DISCOVERY_ALLOWED_PROJECTS=shop-prod,helpdesk-staging   # the demo servers Hornet may observe
HORNET_GATEWAY_ENABLED=true                                    # the SSH front door, on 127.0.0.1:2222
HORNET_AI_PROVIDER=template                                    # offline; `nim` needs NVIDIA_API_KEY
HORNET_AI_SYNTH_ENABLED=true                                   # AI proposals for unsupported requests (5A)
```

With `HORNET_AI_PROVIDER=template` there is no model: every gap is still recorded, with status `fallback` and the
reason "no model provider configured". For real proposals set `HORNET_AI_PROVIDER=nim` and `NVIDIA_API_KEY`.

## 2. Start the demo servers and the API

```bash
python -m scripts.dev demo-up      # builds hornet-twin-base:2, starts shop-prod and helpdesk-staging, migrates
python -m scripts.dev api          # leave running; API on http://127.0.0.1:8000, SSH gateway on 127.0.0.1:2222
```

In a second terminal:

```bash
export KEY=$(grep '^HORNET_ADMIN_API_KEY=' .env | cut -d= -f2-)
API=http://127.0.0.1:8000/api/v1
curl -s $API/health          # {"status":"ok","database":"up","runtime":"up","gateway":"listening:2222"}
```

The interactive API docs are at http://127.0.0.1:8000/docs (click **Authorize** and paste the key).

## 3. Observe the real server and build its twin

```bash
# what Hornet sees, after sanitization (the shop's planted canary secrets are gone)
curl -s -X POST $API/discovery/preview -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"project":"shop-prod"}' | jq '.services[] | {name, role, image, env_classes}'

# build: discover -> sanitize -> plan -> validate -> image -> snapshot (about 10 s with the template provider)
SNAP=$(curl -s -X POST $API/environments/build -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
       -d '{"source":"docker","project":"shop-prod"}' | jq -r .id)
curl -s -X POST $API/environments/$SNAP/publish -H "X-API-Key: $KEY" | jq '{version, status}'
```

A build is `validated` when it returns; publishing makes it the environment every new attacker gets. A published
snapshot can never be changed.

## 4. Be the attacker

```bash
ssh -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@127.0.0.1
```

Any username works. The password is accepted on the 3rd attempt, or at once for a weak one such as `123456`. You
get your own fresh container. Things to try:

```bash
id; hostname; ls -la                     # real commands, real output
cat ~/.pgpass                            # planted (fake) credentials
psql -h db -U shop shop -c '\dt'         # the real psql client against the fake PostgreSQL (uses ~/.pgpass)
psql -h db -U shop shop -c 'select * from customers limit 3'   # use a table name from \dt (see the note below)
curl -s http://web:8080/api/customers | head -c 300
grep requirepass /etc/redis/redis.conf   # then: redis-cli -h cache -a <that password> keys '*'
export DB_PASSWORD=hunter2-not-real      # stored with the value redacted
echo pwned > /tmp/dropper.sh             # recorded as a file change
psql -h db -U shop shop -c 'explain select * from customers'  # beyond the fake's SQL: you get its error
curl -s http://web:8080/admin/login      # unknown route: 404 for you, an AI proposal for the investigator
exit
```

**Table names depend on the provider.** With `HORNET_AI_PROVIDER=template` the shop gets `customers`, `products`,
`orders`. With `nim` the model proposes the schema (one build had `users`, `products`, `purchases`, ...), so run
`\dt` first and use those names in the `select`, `curl .../api/<table>` and `explain` examples.

Things that will not work, by design: network access outside the twin, `sudo`, interactive programs (`vim`, `top`).
Commands that run longer than 5 s are stopped.

## 5. Read back what happened

```bash
SID=$(curl -s "$API/sessions?limit=1" -H "X-API-Key: $KEY" | jq -r '.items[0].id')

# every command: what was typed (credentials redacted), stdout/stderr, exit code. Reading this is audited.
curl -s $API/sessions/$SID/executions -H "X-API-Key: $KEY" | jq '.items[] | {command, exit_code, stdout}'

# AI proposals for the requests the fake services could not answer (separate from the evidence; never served)
curl -s $API/sessions/$SID/executions -H "X-API-Key: $KEY" | jq '.items[] | select(.ai_proposals != []) | {command, ai_proposals}'

# the timeline: commands, files touched in the snapshot graph, fake-service requests (SQL, HTTP, Redis)
curl -s "$API/sessions/$SID/events?limit=200" -H "X-API-Key: $KEY" | jq '.items[] | {event_type, metadata}'

# files the attacker created, changed or deleted
curl -s $API/sessions/$SID/overlay -H "X-API-Key: $KEY" | jq '.items'

# the front door: connections and login attempts (password length + hash only, by default)
curl -s "$API/gateway/events?limit=20" -H "X-API-Key: $KEY" | jq '.items[] | {event_type, source_ip, metadata}'

curl -s $API/analytics/overview -H "X-API-Key: $KEY" | jq
curl -s $API/audit -H "X-API-Key: $KEY" | jq '.items[] | {action, target}'
```

Reconnect with `ssh` and you get a clean twin: `/tmp/dropper.sh` is gone. Your old session is `destroyed`, and its
container was removed when you disconnected.

## 5b. The same in the dashboard

```bash
python -m scripts.dev ui-setup   # once
python -m scripts.dev ui         # http://127.0.0.1:5173, paste the admin key (from .env)
```

- **Home:** the activity list shows your SSH login and every step within about a second; your visit appears as
  a honey hexagon in the hive while you are connected.
- **Attacker sessions → your visit:** read *What happened*, then press *Play* (or *Next*) to replay it. Click a
  command: the green panel is *What really happened*, the dashed violet one the *AI suggestion* (never shown to the
  visitor). The *Environment map* tab shows what you touched and created.
- **Fake environments:** build, *Check it* and *Publish* without curl.
- Words with a dotted underline explain themselves on hover or keyboard focus; *Help* has the full glossary.

To fill the dashboard without typing, `python -m scripts.seed_demo_session --no-export` plays a fixed session.

## 6. Clean up

```bash
# stop the API with Ctrl-C (it destroys any remaining twins), then:
python -m scripts.dev demo-down
python -m scripts.dev purge --days 30   # optional: retention purge of ended sessions (audited)
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `"runtime":"down"` or 503 on build | Docker is not running, or the API user cannot reach the Docker socket |
| 403 on build or preview | Add the project to `HORNET_DISCOVERY_ALLOWED_PROJECTS` and restart the API |
| SSH: connection closed at once | Nothing is published yet, or `HORNET_MAX_ACTIVE_SESSIONS` twins are already running |
| `no such column: execution_request.input_text` | Run `python -m scripts.dev migrate` (an older database) |
