"""Record a demo attacker session through the real SSH gateway, then export it for the dashboard's replay test.

    python -m scripts.seed_demo_session                 # gateway 127.0.0.1:2222, API 127.0.0.1:8000
    python -m scripts.seed_demo_session --no-export     # only play the session (e.g. to fill the dashboard)

Needs the API running with the gateway enabled and a published environment (docs/demo.md). The commands are a fixed
list; service names, ports and database credentials are discovered from the environment itself (the published
snapshot's tree and the twin's ~/.pgpass), never assumed. One command writes an HTML/JS payload so the dashboard's
XSS handling is exercised on real recorded data. Everything recorded is synthetic: the twin's fake data and
credentials, and the commands below.

The export (frontend/src/test/fixtures/demo-session.json) holds what the admin API returned for the session. It
contains no admin key.
"""
import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import asyncssh
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
OUT = ROOT / "frontend" / "src" / "test" / "fixtures" / "demo-session.json"
HTTP_PORTS = {80, 443, 3000, 5000, 8000, 8080, 8443, 8888, 9000}  # as in environment/role_detect.py
XSS = "<img src=x onerror=alert(document.domain)>"


def admin_key() -> str:
    from app.config import get_settings

    return get_settings().admin_api_key


def http_service(api: str, key: str) -> str | None:
    """host:port of a web service of the published environment, from its tree (/services/<name>/tcp-<port>)."""
    h = {"X-API-Key": key}
    snaps = httpx.get(f"{api}/environments", headers=h, timeout=30).raise_for_status().json()
    published = next((s for s in snaps if s["status"] == "published"), None)
    if published is None:
        sys.exit("nothing is published: build and publish an environment first (docs/demo.md)")
    tree = httpx.get(f"{api}/environments/{published['id']}/tree", headers=h, timeout=30).raise_for_status().json()
    for node in tree["nodes"]:
        parts = node["path"].split("/")
        if node["type"] == "endpoint" and len(parts) == 4 and parts[3].startswith("tcp-"):
            port = int(parts[3][4:])
            if port in HTTP_PORTS:
                return f"{parts[2]}:{port}"
    return None


def commands(pgpass: str, web: str | None) -> list[str]:
    cmds = ["id", "hostname", "ls -la", "cat ~/.pgpass"]
    fields = pgpass.strip().splitlines()[0].split(":") if pgpass.strip() else []
    if len(fields) >= 5:
        host, port, db, user = fields[:4]
        cmds += [f"psql -h {host} -p {port} -U {user} {db} -c '\\dt'",
                 f"psql -h {host} -p {port} -U {user} {db} -c 'explain select 1'"]  # beyond the fake: a gap
    if web:
        cmds += [f"curl -s -m 5 http://{web}/health", f"curl -s -m 5 http://{web}/admin/login"]  # the 2nd: a gap
    cmds += ["export DB_PASSWORD=not-a-real-password-123",  # stored redacted
             f"echo '{XSS}' > /tmp/note.html",
             "cat /tmp/note.html",
             "ls -la /tmp"]
    return cmds


async def play(host: str, port: int, cmds_for) -> list[str]:
    async with asyncssh.connect(host, port, username="root", password="123456", known_hosts=None,  # noqa: S106
                                client_keys=None, agent_path=None) as conn:
        pgpass = (await conn.run("cat ~/.pgpass 2>/dev/null", check=False)).stdout or ""
        cmds = cmds_for(str(pgpass))
        for cmd in cmds:  # one exec channel per command, all in this connection's twin
            await conn.run(cmd, check=False)
    return ["cat ~/.pgpass 2>/dev/null", *cmds]


def export(api: str, key: str, typed: list[str], started: datetime) -> Path:
    from app.orchestration.session_manager import command_text

    h = {"X-API-Key": key}
    sessions = httpx.get(f"{api}/sessions?limit=20", headers=h, timeout=30).raise_for_status().json()["items"]
    sid = next(s["id"] for s in sessions if datetime.fromisoformat(s["started_at"].replace("Z", "+00:00"))
               .replace(tzinfo=UTC) >= started.replace(microsecond=0))

    def pages(path: str) -> list:
        out, offset = [], 0
        while True:
            page = httpx.get(f"{api}{path}?limit=200&offset={offset}", headers=h, timeout=60).raise_for_status().json()
            out += page["items"]
            offset += 200
            if len(out) >= page["total"] or not page["items"]:
                return out

    data = {
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": "Recorded by scripts/seed_demo_session.py through the real SSH gateway. Synthetic data only.",
        # what was typed, with credentials redacted exactly as the backend stores commands
        "typed": [command_text(c, 8192) for c in typed],
        "session": httpx.get(f"{api}/sessions/{sid}", headers=h, timeout=30).raise_for_status().json(),
        "executions": pages(f"/sessions/{sid}/executions"),
        "events": pages(f"/sessions/{sid}/events"),
        "tree": httpx.get(f"{api}/sessions/{sid}/tree-state", headers=h, timeout=60).raise_for_status().json(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return OUT


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ssh-host", default="127.0.0.1")
    ap.add_argument("--ssh-port", type=int, default=2222)
    ap.add_argument("--api", default="http://127.0.0.1:8000/api/v1")
    ap.add_argument("--wait", type=float, default=8.0, help="seconds to let AI proposals finish before exporting")
    ap.add_argument("--no-export", action="store_true")
    args = ap.parse_args()

    key = admin_key()
    web = http_service(args.api, key)
    started = datetime.now(UTC)
    typed = asyncio.run(play(args.ssh_host, args.ssh_port, lambda pgpass: commands(pgpass, web)))
    print(f"played {len(typed)} commands through the gateway")
    if args.no_export:
        return
    import time

    time.sleep(args.wait)  # AI synthesis runs after each command, out of band
    out = export(args.api, key, typed, started)
    print(f"exported the session to {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
