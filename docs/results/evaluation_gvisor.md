# Evaluation: runtime measurements (Phase 7.3)

Measured 2026-11-05 11:09 UTC with `python -m scripts.evaluate` on Linux 7.3.1-1-cachyos, Python 3.14.9, Docker 29.9.2, x86_64. gVisor (runsc) runtime, default demo limits, offline builds. Every sample is in `evaluation_gvisor_runtime.csv`.

| Metric | What | Demo server | n | Median (ms) | p95 (ms) | Max (ms) |
|---|---|---|---|---|---|---|
| build | offline | helpdesk-staging | 1 | 3256 | 3256 | 3256 |
| build | offline | shop-prod | 1 | 3130 | 3130 | 3130 |
| command | fake database query | helpdesk-staging | 5 | 200 | 224 | 224 |
| command | fake database query | shop-prod | 5 | 205 | 219 | 219 |
| command | fake website request | helpdesk-staging | 5 | 132 | 145 | 145 |
| command | fake website request | shop-prod | 5 | 128 | 137 | 137 |
| command | list a directory | helpdesk-staging | 5 | 141 | 146 | 146 |
| command | list a directory | shop-prod | 5 | 116 | 132 | 132 |
| command | process | helpdesk-staging | 5 | 128 | 140 | 140 |
| command | process | shop-prod | 5 | 118 | 124 | 124 |
| command | read a file | helpdesk-staging | 5 | 145 | 163 | 163 |
| command | read a file | shop-prod | 5 | 120 | 127 | 127 |
| command | shell builtin | helpdesk-staging | 5 | 125 | 141 | 141 |
| command | shell builtin | shop-prod | 5 | 117 | 119 | 119 |
| command | write a file | helpdesk-staging | 5 | 132 | 136 | 136 |
| command | write a file | shop-prod | 5 | 112 | 116 | 116 |
| session setup | until ready | helpdesk-staging | 10 | 583 | 629 | 629 |
| session setup | until ready | shop-prod | 10 | 532 | 581 | 581 |
| ssh | login + first command (creates the twin) | helpdesk-staging | 1 | 734 | 734 | 734 |
| ssh | login + first command (creates the twin) | shop-prod | 1 | 717 | 717 | 717 |
| ssh | one command, end to end | helpdesk-staging | 20 | 139 | 151 | 156 |
| ssh | one command, end to end | shop-prod | 20 | 115 | 123 | 128 |
