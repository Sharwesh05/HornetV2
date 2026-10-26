# Hornet user guide

Hornet is a decoy server for Docker environments. It looks at one of your real Docker setups (read-only), builds a
believable fake copy with all secrets removed, and lets anyone who logs in over SSH explore their own disposable copy
while everything they do is recorded. This guide covers installing, running and using it on one machine.

> **Safety first.** Everything binds to `127.0.0.1` and Hornet refuses to start with settings that would loosen the
> demo profile. Do not expose it to a network or the internet: that needs the hardening phase (see the
> [threat model](threat-model.md)). Only point it at Docker setups you are allowed to observe.

## 1. Requirements

| | Linux (primary) | Windows |
|---|---|---|
| Docker | Docker Engine with Compose v2 | Docker Desktop with the WSL2 backend (weaker isolation: twins share the Docker Desktop VM's kernel) |
| Python | 3.12 or newer | 3.12 or newer from python.org |
| Node.js | 20 or newer (for the dashboard) | 20 or newer |
| SSH client | `ssh` (OpenSSH) | built into Windows 10/11 (`ssh` in PowerShell) |

All commands below are run from the Hornet folder. They are the same on Windows (PowerShell) except where noted.

## 2. Install (once)

```bash
sudo systemctl start docker          # Linux; on Windows start Docker Desktop
python -m scripts.dev setup          # creates .venv, installs Hornet, creates .env with a random admin key
python -m scripts.dev ui-setup       # installs the dashboard's packages (exact versions)
```

You do not need to activate the virtual environment: `python -m scripts.dev ...` uses it automatically. The two helper
scripts in section 8 need its Python: `.venv/bin/python` on Linux, `.venv\Scripts\python.exe` on Windows.

## 3. Configure (`.env`)

`setup` creates `.env` from `.env.example`. It is git-ignored: keys stay on your machine. Set at least:

```bash
HORNET_DISCOVERY_ALLOWED_PROJECTS=shop-prod,helpdesk-staging   # Docker Compose projects Hornet may observe
HORNET_GATEWAY_ENABLED=true                                    # the decoy's SSH door on 127.0.0.1:2222
```

**AI (optional).** Without a model everything works; AI-written extras are skipped and AI suggestions are recorded
as "No AI answer". With NVIDIA's hosted API:

```bash
HORNET_AI_PROVIDER=nim
NVIDIA_API_KEY=nvapi-...                                       # from build.nvidia.com; never commit it
HORNET_NIM_MODEL=nvidia/nemotron-3.5-super-vl-120b-a12b        # a model that currently answers for your key
HORNET_NIM_SYNTH_MODEL=nvidia/nemotron-3.5-super-vl-120b-a12b
HORNET_AI_SYNTH_ENABLED=true                                   # AI suggestions for requests the fakes cannot answer
```

Hosted model availability changes without notice. Before a demo, check that your model answers:
`.venv/bin/python -m scripts.measure_synthesis --provider nim --timeout 3` (20 small synthetic requests; it prints
how many were answered). `--list-models` shows what the key lists, but some listed models answer "404 Not Found".

Check the result (it refuses to run if a setting is unsafe):

```bash
python -m scripts.dev safety
```

## 4. Start

```bash
python -m scripts.dev demo-up        # builds the twin base image, starts the two demo servers, updates the database
python -m scripts.dev api            # terminal 1: the API on 127.0.0.1:8000 and the SSH door on 127.0.0.1:2222
python -m scripts.dev ui             # terminal 2: the dashboard on http://127.0.0.1:5173
```

Open http://127.0.0.1:5173 and paste the admin key (`HORNET_ADMIN_API_KEY` in `.env`). The key stays in that browser
tab only; reloading signs you out. The sun/monitor/moon buttons switch light, automatic and dark themes.

## 5. Build and publish a fake environment

In the dashboard, **Fake environments**:
1. Enter the Docker Compose project to imitate (e.g. `shop-prod`) and press **Build**. Offline it takes a few
   seconds; with an AI model 2 to 4 minutes (the page shows the elapsed time).
2. Press **Check it** if the new version is not checked yet, then **Publish**. New visitors now get this version.

You only rebuild when nothing is published, the demo server changed, or you want a fresh environment. Published
versions stay published across restarts. **See its files** shows what a visitor will find; everything there is fake.

## 6. Be the visitor (attacker)

```bash
ssh -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@127.0.0.1
```

Any user name works. The password `123456` (or `password`, `admin`, `root`, `toor`) is accepted at once; any other
password on the third try. Each connection gets a brand-new private copy; leaving deletes it. Things to try:

```bash
id; hostname; ls -la; cat ~/.pgpass                    # real commands, fake data
psql -h db -U shop shop -c '\dt'                         # the fake database (names come from ~/.pgpass)
psql -h db -U shop shop -c 'explain select 1'            # something the fake cannot answer: an AI suggestion is kept
curl -s http://web:8080/admin/login                      # an unknown page: also kept for the AI
echo hello > /tmp/note.txt                               # recorded as a new file
exit
```

Database, user and host names differ per environment: use what `cat ~/.pgpass` shows. Long commands are stopped
after 5 seconds, there is no internet inside, and `sudo`, `vim` or `top` are not available: this is by design.

## 7. See what happened

- **Home:** what is going on now, the hive of recent visits (honey = live, red = stopped by a safety limit) and the
  activity feed, which shows each step within about a second.
- **Attacker sessions:** every visit with its status, duration, number of commands and AI suggestions. Open one:
  - **What happened:** a plain-English summary of the evidence (written by Hornet, not AI).
  - **Replay:** press Play, or step with Back/Next. Click a command to inspect it: the green panel *What really
    happened* is the real output; the dashed violet panel *AI suggestion* is what an AI thinks a real server would
    have answered. The visitor never saw it.
  - **Files touched, Environment map, All events:** what changed, what was visited, every recorded event.
- **Help:** how Hornet works and a glossary. Dotted-underlined words explain themselves on hover or keyboard focus.

Everything a visitor typed is shown as plain text and never run in your browser. Passwords inside commands are
hidden when stored.

## 8. Useful extras

```bash
.venv/bin/python -m scripts.seed_demo_session --no-export   # play a fixed visitor session (fills the dashboard)
.venv/bin/python -m scripts.evaluate                        # measure setup time and latency (writes docs/results/)
python -m scripts.dev purge --days 30                       # delete ended visits older than 30 days (audited)
python -m scripts.dev test -rs                              # the test suite (Docker and both demo servers running)
python -m scripts.dev ui-check                              # dashboard lint, types, tests, build
```

The admin API's interactive documentation is at http://127.0.0.1:8000/docs (click *Authorize*, paste the key).

**Stronger isolation with gVisor (Linux only).** gVisor gives each copy its own user-space kernel, so a visitor's
system calls do not reach the host kernel. Install `runsc` (your distribution's package or the official release at
gvisor.dev), register it with `sudo runsc install`, restart Docker, and check with
`docker run --rm --runtime=runsc hello-world`. Then set `HORNET_RUNTIME_DRIVER=gvisor` in `.env`; if Docker has no
`runsc` runtime, Hornet refuses to create copies (it never falls back to runc). Real-Docker tests under gVisor:
`HORNET_TEST_RUNTIME=gvisor python -m scripts.dev test -rs -m docker`. Windows has no gVisor: there the weaker
hardened-Docker driver is the only one.

## 9. Stop

Press Ctrl-C in the API and dashboard terminals (the API removes any remaining copies), then
`python -m scripts.dev demo-down` to stop the demo servers.

## 10. Troubleshooting

| What you see | Cause | Fix |
|---|---|---|
| Build failed: `AI sanitization review failed (AIProviderError)` | The model in `HORNET_NIM_MODEL` does not answer for your key (often HTTP 404 after NVIDIA changes its catalogue). With the default `required` mode Hornet refuses to build without the review | Set a model that answers (section 3), or `HORNET_AI_PROVIDER=template` to build offline; restart the API |
| `docker` errors, or the API health shows `runtime: down` | Docker is not running (e.g. after a reboot) | `sudo systemctl start docker`, then `python -m scripts.dev demo-up` |
| SSH logs in and closes at once | No environment is published, its image was removed (e.g. `docker image prune`), or 5 copies are already running | Publish one (section 5); rebuild if its image is gone |
| `no such column: execution_request.input_text` | The database is older than the code | `python -m scripts.dev migrate` |
| Build refused with 403 | The project is not in `HORNET_DISCOVERY_ALLOWED_PROJECTS` | Add it and restart the API |
| The API refuses to start: `unsafe demo configuration` | A setting would loosen the demo profile (non-loopback, raw commands, a looser limit) | Read the message; `python -m scripts.dev safety` shows the effective values |
| Dashboard says "That key was not accepted" / live feed "refused" | Wrong admin key | Copy `HORNET_ADMIN_API_KEY` from `.env` again |
| Dashboard says "Hornet's API is not answering" | The API is not running | `python -m scripts.dev api` |
| Port 2222, 8000 or 5173 already in use | Another copy is running | Stop it, or change `HORNET_GATEWAY_PORT` / `--port` |

## 11. Windows notes

- Use PowerShell; `python -m scripts.dev ...` commands are identical. The virtual environment's Python is
  `.venv\Scripts\python.exe` (for `seed_demo_session`, `evaluate`, `measure_synthesis`).
- Docker Desktop must use the WSL2 backend and be running before `demo-up`. Twins share Docker Desktop's VM kernel:
  this is weaker isolation than Linux; never treat a Windows setup as safe to expose.
- Inside a twin, `uname -r` shows a `...-microsoft-standard-WSL2` kernel.
