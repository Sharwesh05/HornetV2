# Evaluation: runtime measurements (Phase 7.3)

Measured 2026-10-21 05:51 UTC with `python -m scripts.evaluate` on Linux 7.2.10-1-cachyos, Python 3.14.8, Docker 29.9.1, x86_64. Hardened-Docker runtime, default demo limits, offline builds. Every sample is in `evaluation_runtime.csv`.

| Metric | What | Demo server | n | Median (ms) | p95 (ms) | Max (ms) |
|---|---|---|---|---|---|---|
| build | offline | helpdesk-staging | 1 | 3172 | 3172 | 3172 |
| build | offline | shop-prod | 1 | 3044 | 3044 | 3044 |
| command | fake database query | helpdesk-staging | 5 | 154 | 168 | 168 |
| command | fake database query | shop-prod | 5 | 133 | 162 | 162 |
| command | fake website request | helpdesk-staging | 5 | 110 | 122 | 122 |
| command | fake website request | shop-prod | 5 | 125 | 137 | 137 |
| command | list a directory | helpdesk-staging | 5 | 101 | 127 | 127 |
| command | list a directory | shop-prod | 5 | 109 | 142 | 142 |
| command | process | helpdesk-staging | 5 | 108 | 112 | 112 |
| command | process | shop-prod | 5 | 112 | 121 | 121 |
| command | read a file | helpdesk-staging | 5 | 103 | 116 | 116 |
| command | read a file | shop-prod | 5 | 107 | 140 | 140 |
| command | shell builtin | helpdesk-staging | 5 | 100 | 143 | 143 |
| command | shell builtin | shop-prod | 5 | 113 | 142 | 142 |
| command | write a file | helpdesk-staging | 5 | 111 | 137 | 137 |
| command | write a file | shop-prod | 5 | 103 | 120 | 120 |
| session setup | until ready | helpdesk-staging | 10 | 423 | 473 | 473 |
| session setup | until ready | shop-prod | 10 | 433 | 494 | 494 |
| ssh | login + first command (creates the twin) | helpdesk-staging | 1 | 543 | 543 | 543 |
| ssh | login + first command (creates the twin) | shop-prod | 1 | 565 | 565 | 565 |
| ssh | one command, end to end | helpdesk-staging | 20 | 104 | 119 | 145 |
| ssh | one command, end to end | shop-prod | 20 | 105 | 118 | 143 |

## AI synthesis (from `scripts/measure_synthesis.py`, 3 s limit)

Out of band: the visitor never waits for these calls.

| Model | Requests | Median (ms) | p95 (ms) | Accepted |
|---|---|---|---|---|
| nim:nvidia/nemotron-3-nano-omni-30b-a3b-reasoning | 20 | 538 | 2767 | 8/20 |
| nim:nvidia/nemotron-3-super-120b-a12b | 20 | 973 | 2342 | 17/20 |
| nim:nvidia/nemotron-3.5-super-vl-120b-a12b | 20 | 869 | 3001 | 14/20 |

Hosted-model numbers vary from day to day (free tier, shared capacity, rate limits); see `synthesis_latency.md`.
