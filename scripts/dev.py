"""Cross-platform task runner (AGENTS.md section 3). Run from the repo root with any Python 3.12+:

    python -m scripts.dev setup        create .venv, install the project, create .env (with a random admin key)
    python -m scripts.dev test [args]  run pytest (extra args are passed through)
    python -m scripts.dev lint         ruff + mypy
    python -m scripts.dev migrate      apply database migrations
    python -m scripts.dev api          run the API (and the SSH gateway if HORNET_GATEWAY_ENABLED=true)
    python -m scripts.dev up / down    start / stop the management plane compose stack (Postgres + API container)
    python -m scripts.dev demo-up      build the twin base image, start both demo servers, migrate
    python -m scripts.dev demo-down    stop the demo servers and remove any leftover twins
    python -m scripts.dev purge        delete ended sessions older than HORNET_RETENTION_DAYS (audited)
    python -m scripts.dev safety       show the settings that keep the demo safe (fails if they are unsafe)
    python -m scripts.dev ui-setup     install the dashboard's npm packages (frontend/, exact versions from the lockfile)
    python -m scripts.dev ui           run the dashboard on http://127.0.0.1:5173 (needs `api` running)
    python -m scripts.dev ui-check     dashboard lint, type check, tests and production build

Commands are run with argument lists, never through a shell.
"""
import argparse
import os
import secrets
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
VENV = ROOT / ".venv"
BASE_IMAGE = "hornet-twin-base:2"
DEMO_STACKS = (ROOT / "main-server" / "docker-compose.yml", ROOT / "demo-servers" / "helpdesk" / "docker-compose.yml")
DEMO_PROJECTS = "shop-prod,helpdesk-staging"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def python() -> str:
    """The project's virtualenv interpreter if it exists, otherwise the one running this script."""
    return str(venv_python()) if venv_python().exists() else sys.executable


def run(cmd: list[str], cwd: Path = ROOT, check: bool = True) -> int:
    print("+", " ".join(cmd), flush=True)
    code = subprocess.call(cmd, cwd=cwd)  # noqa: S603  (argument list, no shell)
    if check and code != 0:
        sys.exit(code)
    return code


def need(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        sys.exit(f"'{tool}' was not found on PATH")
    return path


# ---- commands ------------------------------------------------------------------------------------------------
def setup(_args) -> None:
    if not venv_python().exists():
        print(f"creating {VENV}")
        venv.EnvBuilder(with_pip=True).create(VENV)
    run([str(venv_python()), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(venv_python()), "-m", "pip", "install", "-e", ".[dev]"])
    env, example = ROOT / ".env", ROOT / ".env.example"
    if not env.exists():
        text = example.read_text(encoding="utf-8").replace(
            "HORNET_ADMIN_API_KEY=\n", f"HORNET_ADMIN_API_KEY={secrets.token_urlsafe(32)}\n", 1)
        env.write_text(text, encoding="utf-8", newline="\n")
        print("created .env with a random HORNET_ADMIN_API_KEY (read it from .env; it is git-ignored)")
    else:
        print(".env already exists: left unchanged")


def test(args) -> None:
    run([python(), "-m", "pytest", "backend/tests", *args.extra])


def lint(_args) -> None:
    run([python(), "-m", "ruff", "check", "backend", "scripts", "--exclude", "backend/migrations"])
    run([python(), "-m", "mypy"])  # strict on sanitization, ai and orchestration (pyproject.toml)


def migrate(_args) -> None:
    run([python(), "-m", "alembic", "upgrade", "head"], cwd=BACKEND)


def api(args) -> None:
    run([python(), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.port)], cwd=BACKEND)


def up(_args) -> None:
    run([need("docker"), "compose", "up", "-d", "--build"])


def down(_args) -> None:
    run([need("docker"), "compose", "down"])


def demo_up(_args) -> None:
    docker = need("docker")
    if run([docker, "info", "--format", "{{.ServerVersion}}"], check=False) != 0:
        sys.exit("Docker is not reachable: start it first (Linux: sudo systemctl start docker)")
    run([docker, "build", "-t", BASE_IMAGE, str(ROOT / "twin-image")])
    for stack in DEMO_STACKS:
        run([docker, "compose", "-f", str(stack), "up", "-d", "--build"])
    migrate(_args)
    print(f"\nDemo servers are up. In .env set HORNET_DISCOVERY_ALLOWED_PROJECTS={DEMO_PROJECTS} and, for SSH,\n"
          "HORNET_GATEWAY_ENABLED=true; then `python -m scripts.dev api`. Walkthrough: docs/demo.md")


def demo_down(_args) -> None:
    docker = need("docker")
    for stack in DEMO_STACKS:
        run([docker, "compose", "-f", str(stack), "down"], check=False)
    # twins carry this label (app/runtime/docker_driver.py LABEL); the API removes them too, on its next start
    ids = subprocess.run([docker, "ps", "-aq", "--filter", "label=com.hornet.managed=true"],  # noqa: S603
                         capture_output=True, text=True, check=False).stdout.split()
    if ids:
        run([docker, "rm", "-f", "-v", *ids], check=False)
    print(f"removed {len(ids)} leftover twin container(s)")


def safety(_args) -> None:
    if venv_python().exists() and Path(sys.prefix).resolve() != VENV.resolve():
        sys.exit(run([python(), "-m", "scripts.dev", "safety"], check=False))  # needs the project's packages
    sys.path.insert(0, str(BACKEND))
    from pydantic import ValidationError

    from app.config import Settings

    try:
        summary = Settings().safety_summary()
    except ValidationError as exc:
        sys.exit(f"UNSAFE configuration, the API would refuse to start:\n{exc}")
    width = max(len(k) for k in summary)
    for k, v in summary.items():
        print(f"{k:<{width}}  {v}")


def ui_setup(_args) -> None:
    run([need("npm"), "ci"], cwd=FRONTEND)


def ui(_args) -> None:
    run([need("npm"), "run", "dev"], cwd=FRONTEND)


def ui_check(_args) -> None:
    npm = need("npm")
    for script in ("lint", "typecheck", "test", "build"):
        run([npm, "run", script], cwd=FRONTEND)


def purge(args) -> None:
    # (compare prefixes, not executables: a venv's python is often a symlink to the system one)
    if venv_python().exists() and Path(sys.prefix).resolve() != VENV.resolve():
        # needs the project's packages: re-run with the virtualenv's interpreter
        sys.exit(run([python(), "-m", "scripts.dev", "purge", *(["--days", str(args.days)] if args.days else [])],
                     check=False))
    sys.path.insert(0, str(BACKEND))
    from app import retention
    from app.config import get_settings
    from app.db import get_sessionmaker

    days = args.days or get_settings().retention_days
    with get_sessionmaker()() as db:
        counts = retention.purge(db, days, actor="cli-retention")
    print(f"purged sessions ended more than {days} day(s) ago: " + ", ".join(f"{k}={v}" for k, v in counts.items()))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.dev", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn, helptext in (
        ("setup", setup, "create .venv, install, create .env"),
        ("lint", lint, "ruff check and mypy"),
        ("migrate", migrate, "alembic upgrade head"),
        ("up", up, "start the management compose stack"),
        ("down", down, "stop the management compose stack"),
        ("demo-up", demo_up, "build the base image, start both demo servers, migrate"),
        ("demo-down", demo_down, "stop the demo servers, remove leftover twins"),
        ("safety", safety, "show the demo safety settings"),
        ("ui-setup", ui_setup, "npm ci in frontend/"),
        ("ui", ui, "run the dashboard dev server"),
        ("ui-check", ui_check, "dashboard lint, typecheck, tests, build"),
    ):
        sub.add_parser(name, help=helptext).set_defaults(fn=fn)
    sub.add_parser("test", help="run pytest; extra args are passed through").set_defaults(fn=test)
    a = sub.add_parser("api", help="run the API on 127.0.0.1")
    a.add_argument("--port", type=int, default=8000)
    a.set_defaults(fn=api)
    p = sub.add_parser("purge", help="delete ended sessions older than the retention period (audited)")
    p.add_argument("--days", type=int, default=None, help="override HORNET_RETENTION_DAYS")
    p.set_defaults(fn=purge)
    args, extra = parser.parse_known_args(argv)  # only `test` accepts extra arguments (passed on to pytest)
    if extra and args.command != "test":
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    args.extra = extra
    args.fn(args)


if __name__ == "__main__":
    main()
