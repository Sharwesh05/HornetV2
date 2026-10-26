# Demo script (about 9 minutes)

For the final presentation. Every step shows something that is implemented and tested; the wording about AI is
deliberately exact: **AI suggestions are recorded for investigators and never shown to the attacker.** Do not say
that the AI answers attackers live; that part (5B) is not built.

Layout: the dashboard in a browser on one side, two terminals on the other (T1 runs Hornet, T2 is "the attacker").

## Before the talk (15 minutes ahead)

```bash
sudo systemctl start docker
python -m scripts.dev demo-up                       # base image, both demo servers, database up to date
python -m scripts.dev safety                        # read it once: loopback only, redacted commands, demo limits
.venv/bin/python -m scripts.measure_synthesis --provider nim --timeout 3    # is today's model answering?
python -m scripts.dev api                           # T1 (leave running)
python -m scripts.dev ui                            # another terminal (leave running)
```

1. Open http://127.0.0.1:5173, paste the admin key, choose the light theme (it projects better).
2. **Fake environments:** if no version says "Live: new visitors get this one", build `shop-prod`, *Check it*,
   *Publish* now (2 to 4 minutes with the model). Do not build during the talk.
3. Optional warm-up so the hive is not empty: `.venv/bin/python -m scripts.seed_demo_session --no-export`.
4. In T2, type but do not run: `ssh -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@127.0.0.1`.
5. `cat ~/.pgpass` inside a test login shows this build's database user and name: note them for step C.

**If the model check fails** (0 answered, HTTP 404/429): set `HORNET_AI_PROVIDER=template` in `.env`, restart T1, and
in step D say "today the AI service is offline, so Hornet records the gap without a suggestion". Everything else is
unchanged.

## A. The idea (1 minute) — Home page

> "Intruders who get into a server rarely show us how they work. Hornet is a decoy that looks like one of our real
> Docker servers. Anyone who logs in gets their own disposable copy. Their commands really run, nothing they do can
> leave that copy, and we record everything."

Point at the headline sentence and the hive: "each hexagon is one visit; honey means someone is inside right now."

## B. The fake environment (1.5 minutes) — Fake environments

> "Hornet looked at this real shop setup, read-only. It never saw the values of its environment variables, removed
> every secret it could find, and built a believable fake: fake users, data and passwords that are consistent with
> each other."

- Show the live version card, then **See its files**: open `/home/user` and point at `.pgpass` with the key icon:
  "fake passwords, planted on purpose."
- "We planted canary secrets in the real server. Tests show none of them reaches the copy, the database, the logs or
  any prompt sent to the AI."

## C. The attack (3 minutes) — T2, with the Home page visible

Run the prepared `ssh` command; password `123456`. As the activity feed fills in, narrate it ("logged in as root",
"pointed at…").

```bash
id                                   # a real Linux process answers
cat ~/.pgpass                        # the bait
psql -h db -U shop shop -c '\dt'     # real psql against the fake database (use the user/db from ~/.pgpass)
psql -h db -U shop shop -c 'explain select 1'    # beyond what the fake database understands
curl -s http://web:8080/admin/login; echo   # a page the fake website does not have
echo pwned > /tmp/dropper.sh         # a file of their own
curl -sS -m 3 https://example.com    # fails: "Could not resolve host", no way out of the copy
exit
```

> "Each command took about a tenth of a second. The copy had no network, strict limits, and it was deleted the
> moment they left."

## D. What happened (2.5 minutes) — Attacker sessions → the visit just made

1. **What happened** summary: "written by Hornet from the evidence, no AI." Point at the fake-password line and the
   new file.
2. **Replay:** press *Start*, then *Play*; let three or four commands appear.
3. Click the `explain` command:
   > "Green, *What really happened*: the real output, exactly what the attacker saw: the fake database's error.
   > Violet, *AI suggestion*: afterwards Hornet asked an AI what a real PostgreSQL server would have answered,
   > checked the answer in code, and kept it for us. The attacker never saw it. The two are never mixed."
4. **Environment map:** "what they touched, and the file they created, only in their copy."

## E. Safety and numbers (1 minute) — T1 or a slide

`python -m scripts.dev safety` (or show its output): "Hornet refuses to start with unsafe settings."

- Commands: about 0.1 s end to end; a fresh copy: about 0.5 s; AI suggestions: about 1 s, off the attacker's path.
- 517 backend tests (44 against real Docker: no egress by any protocol, fork bombs, cross-visitor isolation) and
  64 dashboard tests (including XSS: attacker text never runs in our browser).

## F. Honest limits and next step (30 seconds)

> "A careful attacker can tell it is a decoy, for example from the kernel version, and the copies share the host's
> kernel. That is why the next, mandatory phase moves the copies to gVisor and runs a full escape test suite before
> anything is exposed to the internet."

## Questions you may get

| Question | Answer |
|---|---|
| Can the attacker break out? | Not in our tests (capabilities, socket, mounts, egress, exhaustion). The copies share the kernel, so a kernel exploit is the risk; gVisor is the next phase. |
| Does the AI see our secrets? | No. Secrets are removed by deterministic code first, the AI sees only redacted metadata, code checks again afterwards, and tests check every prompt for planted canaries. |
| What if the AI is wrong or manipulated? | Its answer is validated in code (no invented tables, no fake successes for writes, no secrets) and it is never shown to the attacker or stored as evidence. |
| Why not let the AI answer the attacker? | That would let a model shape the evidence. We kept it out of the attacker's path; live answering is an optional extension. |
| Does it only work for this shop? | No. The same tests run against a second, deliberately different server, plus 49 unusual shapes. |
| Can they tell it is a honeypot? | A careful one can; see `docs/limitations.md`. Stealth was out of scope; isolation and evidence were the priorities. |

## Fallbacks

- **SSH fails or the hive stays empty:** check T1 for errors; `python -m scripts.dev safety`; publish a version.
- **The model is down mid-demo:** gaps are recorded with "No AI answer"; say so and move on.
- **Anything else:** `.venv/bin/python -m scripts.seed_demo_session --no-export` plays a recorded attacker session
  through the real gateway; continue at step D with that visit.
