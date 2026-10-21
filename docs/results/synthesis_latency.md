# AI synthesis latency (Phase 5A, task 5.8)

Measured 2026-10-19 with `python -m scripts.measure_synthesis`: 20 fixed requests that the fake services flag as
`unsupported` (10 PostgreSQL, 6 Redis, 4 HTTP), each run through the real worker (context builder, hard timeout,
validator, storage). Only synthetic data was sent: the demo schema's table and column names and the requests listed
in the script. Hosted NVIDIA API (`integrate.api.nvidia.com`), free tier, from a home connection.

The attacker never waits for these calls: synthesis runs after the command's reply (Phase 5A is out of band).

## Result: the default model

`nvidia/nemotron-3-super-120b-a12b`, thinking off, no guided decoding, 3 s timeout:

| Run | Median | p95 | Max | Accepted | Rejected | Timeout | Fallback |
|---|---|---|---|---|---|---|---|
| 1 | 1252 ms | 2033 ms | 2838 ms | 20 | 0 | 0 | 0 |
| 2 | 973 ms | 2342 ms | 2998 ms | 17 | 0 | 0 | 3 (HTTP 429, rate limit) |

Raw rows of the last run: `synthesis_nim-nvidia_nemotron-3-super-120b-a12b.csv`. Pipeline overhead without a model
(context + validation + storage) is at most 1 ms per request (`synthesis_fake.csv`).

## How the default was chosen

| Finding | Effect |
|---|---|
| Of the 81 models the key lists, most return HTTP 404 ("Function not found for account") or time out; 5 answered (all Nemotron 3 / reasoning-style, plus step-5-preview) | Only listed models that actually answer can be used: probe with `--list-models` and a test call |
| Reasoning models think before answering: Nemotron 3 Super took 8.9 s for one Redis request | `chat_template_kwargs: {"enable_thinking": false}` brings it to about 1 s (`NimProvider(thinking=False)`) |
| NVIDIA's schema-constrained decoding (`nvext.guided_json`) cost Nemotron 3 Super 11.3 s vs 1.5 s without it, and made Nemotron 3 Nano Omni return HTTP 503 | Synthesis sends the schema in the prompt only (`guided_json=False`); the validator enforces it in code |
| Nemotron 3 Nano Omni (thinking off, no guided decoding): median 538 ms but 10 of 20 provider errors | Not reliable enough on this account |
| step-5-preview, Nemotron 3.5 Lightning, GLM 5.3 Flash: most calls over 8 s | Too slow for a 3 s budget |

Builds (discovery sanitization review, schema and document proposals) are unchanged: they keep `HORNET_NIM_MODEL`,
the model's default thinking and guided decoding, and a 90 s timeout.

## Quality notes (not measured systematically)

The validator checks safety and consistency, not realism. A spot check of accepted answers: `CLIENT LIST`,
`MODULE LOAD`, `GET /admin/login` and `GET /.env` got plausible errors; `EXPLAIN select * from customers` got table
rows instead of a `QUERY PLAN`. That answer is safe (known table and columns, example data) but unrealistic.
Spot checks also found two validator bugs, both fixed with regression tests: a CTE name was taken for an unknown
table, and a real SQLSTATE (`42809`) was missing from the allowlist.

## Reproduce

```bash
python -m scripts.measure_synthesis --list-models                          # what the key lists
python -m scripts.measure_synthesis --provider nim --timeout 3             # default synthesis model
python -m scripts.measure_synthesis --provider nim --model <id> --thinking # compare with thinking on
```
Free-tier rate limits (HTTP 429) appear when runs are repeated back to back; they are recorded as `fallback`.
