# Demo commands

Every command for showing Hornet to a panel, in order, using the shop demo server. For the full speaking script and
likely questions, see [demo-script.md](demo-script.md).

Run everything from the repository root. Commands work in fish, zsh and bash. Keep four terminals open: **T1** API,
**T2** dashboard, **T3** "the real server" and **T4** "the attacker".

*Checked on 2026-11-05 on Linux: sections 2 and 4 ran for real, the attack through the real SSH gateway under gVisor
on an offline build. The dashboard steps and a build with the AI model were not re-run, so rehearse once before the
panel.*

## What is what

| Name | What it is | Where |
|---|---|---|
| **Shop** (`shop-prod`) | The pretend "real company server": a small online shop. All its data is fake | `main-server/` |
| `shop-web` | Its website, on http://127.0.0.1:8080 | Docker container |
| `shop-db` | Its PostgreSQL database: `customers` and `products` | Docker container |
| `shop-cache` | Its Redis cache: keeps the product list for 30 seconds | Docker container |
| **Canaries** | Fake secrets planted in the shop, like `CANARY-DB-PASSWORD-7f3a`, to prove Hornet never copies secrets | Shop settings |
| **Twin** | The disposable copy each attacker gets: a container with no network, deleted when they leave | Created by Hornet |
| `db`, `cache`, `web` | Hornet's **fake** services, inside the twin only, on `127.0.1.x`. They never reach the real containers | Inside the twin |
| **Gateway** | Hornet's SSH front door, on `127.0.0.1:2222`. Only this machine can reach it | Inside the API process |

`helpdesk-staging` is a second demo server that the tests use. `demo-up` starts it too; you don't need to show it.

## 1. Prepare (15 minutes before)

```
sudo systemctl start docker
python -m scripts.dev demo-up
python -m scripts.dev safety
.venv/bin/python -m scripts.measure_synthesis --provider nim --timeout 3
```

- `safety` should say `twin runtime  gVisor` (`HORNET_RUNTIME_DRIVER=gvisor` in `.env`). If it says `login passwords
  stored  as text`, set `HORNET_GATEWAY_LOG_PASSWORDS=false` in `.env` before starting the API.
- If the AI check answers nothing, set `HORNET_AI_PROVIDER=template` in `.env`. Everything else still works, but AI
  suggestions show "No AI answer".

Start Hornet and leave both running:

```
python -m scripts.dev api      # T1
python -m scripts.dev ui       # T2
```

Open http://127.0.0.1:5173 and paste `HORNET_ADMIN_API_KEY` from `.env`. Under **Fake environments**, check that a
`shop-prod` version is live. If not: **Build a new version** for `shop-prod`, then **Check it**, then **Publish**
(2 to 4 minutes with the AI). Don't build during the talk.

Optional, so the dashboard isn't empty:

```
.venv/bin/python -m scripts.seed_demo_session --no-export
```

## 2. The real server (T3)

> "This is the company's real server: a website, a database and a cache. Hornet is allowed to look at it, read-only."

```
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
curl -s http://127.0.0.1:8080/api/products; echo
docker exec shop-db psql -U shop shop -c '\dt'
docker exec shop-db psql -U shop shop -c 'select * from customers'
docker exec shop-cache redis-cli keys '*'
```

> "It has secrets. These are planted fakes, so we can prove Hornet never copies them."

```
docker inspect shop-web --format '{{range .Config.Env}}{{println .}}{{end}}'
```

## 3. The fake environment (dashboard)

> "From that read-only look, Hornet built a twin with the same shape, but fake users, data and passwords."

Go to **Fake environments**, open the live version, then **See its files**, then `/home/user/.pgpass`: "fake passwords,
planted on purpose."

Prove the canaries never reached Hornet's database. This should print `0`:

```
grep -a -c CANARY hornet_dev.db
```

## 4. The attack (T4)

Keep the dashboard's **Home** page visible; the activity feed fills in live.

```
ssh -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@127.0.0.1
```

Password: `123456`.

Look around. Commands really run, as an ordinary user:

```bash
hostname; id; uname -r
cat /etc/hosts
ls -la ~
cat ~/NOTES.txt
cat ~/.bash_history
```

`whoami` says `user` whatever name you logged in with. `uname -r` prints `4.4.0`, gVisor's own kernel, not the real one.

Find the bait and use it:

```bash
cat ~/.pgpass
psql -h db -U shop shop -c '\dt'
psql -h db -U shop shop -c 'select * from customers limit 5'
grep requirepass /etc/redis/redis.conf
redis-cli -h cache -a '<password from the line above>' --no-auth-warning keys '*'
curl -s http://web:8080/api/products; echo
```

`~/.pgpass` reads `host:port:database:user:password`; use its values if they differ. If there's no `customers` table,
use any table `\dt` lists.

Reach what the fake services don't support. These become "gaps" that the AI looks at afterwards:

```bash
psql -h db -U shop shop -c 'explain select 1'
curl -s http://web:8080/admin/login; echo
```

Leave a file, then try to escape:

```bash
echo pwned > /tmp/dropper.sh
sudo id
curl -sS -m 3 https://example.com
ls /var/run/docker.sock
mount -t tmpfs none /mnt
exit
```

| Command | Expected result |
|---|---|
| `sudo id` | `sudo: command not found` |
| `curl ... example.com` | `Could not resolve host`: no network at all |
| `ls /var/run/docker.sock` | `No such file or directory` |
| `mount ...` | `must be superuser to use mount` |

Optional: to prove gVisor, run this in T3 **while still logged in**. The second command prints `runsc`:

```
docker ps --filter label=com.hornet.managed=true --format '{{.ID}}'
docker inspect -f '{{.HostConfig.Runtime}}' <ID from above>
```

After `exit`, the first command lists nothing: the twin has been deleted.

## 5. What happened (dashboard)

Go to **Attacker sessions** and open the visit you just made.

1. **What happened:** "written by code from the evidence, no AI."
2. **Replay:** press **Start**, then **Play**.
3. Click the `explain` command: green is what the attacker really saw; violet is the AI suggestion, made afterwards,
   checked in code and **never shown to the attacker**.
4. **Environment map:** what they touched, and their new file, only in their copy.

## 6. Safety (T3)

```
python -m scripts.dev safety
env HORNET_ENV=prod python -m scripts.dev safety
```

The second command prints `UNSAFE configuration, the API would refuse to start`. It names one problem at a time (first,
SQLite in production).

Say numbers carefully: command and start-up timings in `docs/results/evaluation.md` were measured on hardened Docker,
**not** gVisor. Don't say the AI answers attackers live; it doesn't.

## 7. Stop

Press Ctrl-C in T1 and T2 (the API removes any remaining twins), then:

```
python -m scripts.dev demo-down
```

## If something goes wrong

| Problem | What to do |
|---|---|
| SSH refused | Is T1 running? Does `python -m scripts.dev safety` show `ssh gateway  on`? (`HORNET_GATEWAY_ENABLED=true`) |
| SSH connects, then closes | No version is live, or a limit was hit. Publish a version; see T1's output |
| A build fails with the AI | Set `HORNET_AI_PROVIDER=template` in `.env`, restart T1, build again (takes seconds) |
| `gVisor` errors at login | Run `docker run --rm --runtime=runsc hello-world`. To continue quickly, set `HORNET_RUNTIME_DRIVER=docker` in `.env` and restart T1 (then `uname -r` shows the real kernel) |
| Anything else mid-talk | Run `.venv/bin/python -m scripts.seed_demo_session --no-export`, then continue at section 5 with that visit |
