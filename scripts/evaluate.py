"""Phase 7.3: basic measurements for the report.

    python -m scripts.evaluate                    # all allowlisted demo stacks that are running; 10 sessions, 5 reps
    python -m scripts.evaluate --sessions 20 --reps 10

Needs Docker, hornet-twin-base:2 and the demo stacks (python -m scripts.dev demo-up). Uses a throwaway SQLite
database and builds offline (template provider), so runtime numbers are not mixed with model latency. Measures:

  build          discover -> sanitize -> generate -> validate -> image, per demo stack
  session setup  create a twin until it is ready for the first command (includes starting the fake services)
  command        one command through the session manager (run + filesystem diff + evidence written), by kind
  ssh            one command through the real SSH gateway with a real SSH client (what a visitor experiences)

AI latency and acceptance come from scripts/measure_synthesis.py (docs/results/synthesis_*.csv) and are folded into
the summary. Output: docs/results/evaluation_runtime.csv (every sample) and docs/results/evaluation.md (summary).
"""
import argparse
import asyncio
import csv
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
RESULTS = ROOT / "docs" / "results"
BASE_IMAGE = "hornet-twin-base:2"
DEMO_PROJECTS = ("shop-prod", "helpdesk-staging")


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, round(p * (len(s) - 1)))]


HTTP_PORTS = {80, 443, 3000, 5000, 8000, 8080, 8443, 8888, 9000}  # as in environment/role_detect.py


def web_service(sf, snapshot_id) -> str | None:
    """host:port of a web service of the snapshot, from its endpoint nodes (/services/<name>/tcp-<port>)."""
    from sqlalchemy import select

    from app.models import VirtualNode

    with sf() as db:
        for path in db.scalars(select(VirtualNode.virtual_path).where(VirtualNode.snapshot_id == snapshot_id,
                                                                        VirtualNode.node_type == "endpoint")):
            parts = path.split("/")
            if len(parts) == 4 and parts[3].startswith("tcp-") and int(parts[3][4:]) in HTTP_PORTS:
                return f"{parts[2]}:{parts[3][4:]}"
    return None


def command_kinds(pgpass: str, web: str | None) -> dict[str, str]:
    kinds = {"shell builtin": "echo hello", "process": "id", "read a file": "cat /etc/passwd",
             "write a file": f"echo x > /tmp/eval-{uuid.uuid4().hex[:6]}", "list a directory": "ls -la /etc"}
    fields = pgpass.strip().splitlines()[0].split(":") if pgpass.strip() else []
    if len(fields) >= 5:
        kinds["fake database query"] = f"psql -h {fields[0]} -U {fields[3]} {fields[2]} -tAc 'select 1'"
    if web:
        kinds["fake website request"] = f"curl -s -m 3 http://{web}/health"
    return kinds


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sessions", type=int, default=10, help="twins to create for the setup-time measurement")
    ap.add_argument("--reps", type=int, default=5, help="repetitions per command kind")
    args = ap.parse_args()

    import docker
    from sqlalchemy.orm import sessionmaker

    import app.immutability  # noqa: F401
    from app import services
    from app.ai.provider import TemplateProvider
    from app.config import Settings
    from app.db import Base, make_engine
    from app.discovery.docker_inventory import ReadOnlyDocker
    from app.environment import pipeline
    from app.environment.image_builder import DockerImageBuilder
    from app.orchestration.session_manager import SessionManager
    from app.runtime.docker_driver import HardenedDockerDriver
    from app.runtime.policy import ExecutionPolicy

    client = docker.from_env()
    running = {c.labels.get("com.docker.compose.project") for c in client.containers.list()}
    projects = [p for p in DEMO_PROJECTS if p in running]
    if not projects:
        sys.exit("no demo stack is running: python -m scripts.dev demo-up")

    samples: list[tuple[str, str, str, float]] = []  # (project, metric, detail, ms)
    tmp = Path(tempfile.mkdtemp(prefix="hornet-eval-"))
    for project in projects:
        engine = make_engine(f"sqlite:///{(tmp / f'{project}.db').as_posix()}")
        Base.metadata.create_all(engine)
        sf = sessionmaker(bind=engine, expire_on_commit=False)
        builder = DockerImageBuilder(client)
        print(f"[{project}] building (offline template provider)...", flush=True)
        t = time.monotonic()
        with sf() as db:
            snap = pipeline.build_from_docker(
                db, docker=ReadOnlyDocker(client), image_builder=builder, provider=TemplateProvider(), project=project,
                allowed_projects={project}, base_image=BASE_IMAGE, build_dir=tmp / project, actor="evaluate")
            samples.append((project, "build", "offline", (time.monotonic() - t) * 1000))
            services.publish_snapshot(db, snap)
            db.commit()
            image = snap.validation_report["build"]["image"]["digest"]
        mgr = SessionManager(HardenedDockerDriver(client), sf, ExecutionPolicy(), BASE_IMAGE)
        try:
            print(f"[{project}] {args.sessions} session setups...", flush=True)
            for _ in range(args.sessions):
                t = time.monotonic()
                s = mgr.create_session()
                samples.append((project, "session setup", "until ready", (time.monotonic() - t) * 1000))
                mgr.destroy_session(s.id)

            s = mgr.create_session()
            pgpass = mgr.execute(s.id, "cat ~/.pgpass 2>/dev/null").stdout
            kinds = command_kinds(pgpass, web_service(sf, snap.id))
            print(f"[{project}] {len(kinds)} command kinds x {args.reps}...", flush=True)
            for kind, cmd in kinds.items():
                for _ in range(args.reps):
                    t = time.monotonic()
                    mgr.execute(s.id, cmd, stateful=True)
                    samples.append((project, "command", kind, (time.monotonic() - t) * 1000))
            mgr.destroy_session(s.id)

            print(f"[{project}] ssh round trips through the gateway...", flush=True)
            cfg = Settings(admin_api_key="x" * 20, _env_file=None, gateway_accept_after=1,
                           gateway_host_key_path=str(tmp / "host_key"))
            samples += [(project, "ssh", d, ms) for d, ms in asyncio.run(ssh_round_trips(mgr, cfg, sf, args.reps * 4))]
        finally:
            mgr.shutdown()
            mgr.reconcile()
            builder.remove(image)

    RESULTS.mkdir(parents=True, exist_ok=True)
    with (RESULTS / "evaluation_runtime.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["project", "metric", "detail", "ms"])
        w.writerows((p, m, d, f"{ms:.1f}") for p, m, d, ms in samples)
    write_summary(samples)
    print(f"written: docs/results/evaluation_runtime.csv, docs/results/evaluation.md ({len(samples)} samples)")


async def ssh_round_trips(mgr, cfg, sf, n: int) -> list[tuple[str, float]]:
    import asyncssh

    from app.gateway.server import start_gateway

    out = []
    gw = await start_gateway(mgr, cfg, sf, port=0)
    try:
        t = time.monotonic()
        async with asyncssh.connect("127.0.0.1", gw.port, username="eval", password="pw", known_hosts=None,
                                    client_keys=None, agent_path=None) as conn:
            await conn.run("true", check=False)  # the first command also creates the visitor's twin
            out.append(("login + first command (creates the twin)", (time.monotonic() - t) * 1000))
            for _ in range(n):
                t = time.monotonic()
                await conn.run("echo hello", check=False)
                out.append(("one command, end to end", (time.monotonic() - t) * 1000))
    finally:
        await gw.close()
    return out


def synthesis_rows() -> list[tuple[str, int, float, float, int]]:
    """(model, requests, median ms, p95 ms, accepted) for each real-model synthesis measurement on file."""
    rows = []
    for f in sorted(RESULTS.glob("synthesis_nim-*.csv")):
        data = list(csv.DictReader(f.open(encoding="utf-8")))
        if not data:
            continue
        lat = [float(r["latency_ms"]) for r in data]
        rows.append((data[0]["provider"], len(data), statistics.median(lat), pct(lat, 0.95),
                     sum(r["status"] == "accepted" for r in data)))
    return rows


def write_summary(samples) -> None:
    groups: dict[tuple[str, str, str], list[float]] = {}
    for p, m, d, ms in samples:
        groups.setdefault((m, d, p), []).append(ms)
    docker_version = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True,
                                    text=True, check=False).stdout.strip()
    lines = [
        "# Evaluation: runtime measurements (Phase 7.3)",
        "",
        f"Measured {datetime.now(UTC):%Y-%m-%d %H:%M} UTC with `python -m scripts.evaluate` on {platform.system()} "
        f"{platform.release()}, Python {platform.python_version()}, Docker {docker_version or '?'}, "
        f"{platform.machine()}. Hardened-Docker runtime, default demo limits, offline builds. Every sample is in "
        "`evaluation_runtime.csv`.",
        "",
        "| Metric | What | Demo server | n | Median (ms) | p95 (ms) | Max (ms) |",
        "|---|---|---|---|---|---|---|",
    ]
    for (m, d, p), v in sorted(groups.items()):
        lines.append(f"| {m} | {d} | {p} | {len(v)} | {statistics.median(v):.0f} | {pct(v, 0.95):.0f} | {max(v):.0f} |")
    syn = synthesis_rows()
    if syn:
        lines += ["", "## AI synthesis (from `scripts/measure_synthesis.py`, 3 s limit)", "",
                  "Out of band: the visitor never waits for these calls.", "",
                  "| Model | Requests | Median (ms) | p95 (ms) | Accepted |", "|---|---|---|---|---|"]
        lines += [f"| {m} | {n} | {med:.0f} | {p95:.0f} | {acc}/{n} |" for m, n, med, p95, acc in syn]
    lines += ["", "Hosted-model numbers vary from day to day (free tier, shared capacity, rate limits); see "
              "`synthesis_latency.md`."]
    (RESULTS / "evaluation.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
