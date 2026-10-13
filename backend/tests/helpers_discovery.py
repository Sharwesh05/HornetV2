"""Fakes for Phase 3 tests: a Docker environment stuffed with canary secrets in every place a real
secret could hide, and an image builder that never touches Docker."""
import hashlib
from pathlib import Path

CANARIES = {
    "db_password": "CANARY-DB-PASSWORD-7f3a",
    "admin_token": "CANARY-ADMIN-TOKEN-91c2",
    "stripe": "sk_test_CANARY0000000000000000",
    "label_secret": "CANARY-LABEL-SECRET-55aa",
    "cmd_token": "CANARY-CMD-TOKEN-77bb",
    "healthcheck_pw": "CANARY-HC-PASS-33cc",
    "aws_key": "AKIAABCDEFGHIJKLMNOP",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJDQU5BUlkifQ.c2lnbmF0dXJlQ0FOQVJZ",
    "email": "ops-lead@corp-canary.example",
    "private_ip": "172.18.0.77",
    "host_path": "C:\\Users\\canary-user\\secret\\shop",
    "registry": "registry.canary-corp.local:5000",
    "fs_secret_path": "/home/canaryuser/.aws/credentials",
}
ALL_CANARY_STRINGS = tuple(CANARIES.values())

# Sensitive in a way no regex can know: only a model (or a human) would flag these.
AI_ONLY = {
    "person": "Dana Whitfield",
    "internal_url": "billing.corp-canary.example",
    "account": "acct-8841-canary",
}


def _container(cid, name, service, image, env, ports, cmd=None, entrypoint=None, labels=None, health=None,
               user=None, workdir=None, mounts=None, networks=("shop-prod_shopnet",), bindings=None):
    base_labels = {
        "com.docker.compose.project": "shop-prod",
        "com.docker.compose.service": service,
        "com.docker.compose.project.working_dir": CANARIES["host_path"],
        "com.docker.compose.config-hash": "abc123",
    }
    base_labels.update(labels or {})
    return {
        "summary": {"Id": cid, "Labels": base_labels},
        "inspect": {
            "Id": cid,
            "Name": "/" + name,
            "Config": {
                "Image": image,
                "Env": env,  # contains VALUES with canaries: discovery must keep names only
                "Cmd": cmd, "Entrypoint": entrypoint, "Labels": base_labels,
                "ExposedPorts": {f"{p}/tcp": {} for p in ports},
                "User": user, "WorkingDir": workdir,
                "Healthcheck": {"Test": health} if health else None,
            },
            "HostConfig": {"PortBindings": bindings or {}},
            "NetworkSettings": {"Networks": {n: {} for n in networks}},
            "Mounts": mounts or [],
        },
    }


def shop_containers():
    return [
        _container(
            "a" * 64, "shop-web", "web", f"{CANARIES['registry']}/team/shop-web:1.0",
            [f"DATABASE_URL=postgresql://shop:{CANARIES['db_password']}@db:5432/shop",
             "REDIS_URL=redis://cache:6379/0",
             f"ADMIN_TOKEN={CANARIES['admin_token']}", f"STRIPE_API_KEY={CANARIES['stripe']}",
             "LOG_FILE=/var/log/shop/app.log", "PORT=8080", "ACME_LICENSE=lic-canary-value"],
            [8080],
            cmd=["gunicorn", "--bind", f"{CANARIES['private_ip']}:8080", "--token", CANARIES["cmd_token"], "app.server:app"],
            entrypoint=["/entry.sh", f"--admin-contact={CANARIES['email']}"],
            labels={"team.api_secret": f"password={CANARIES['label_secret']}", "ops.aws": CANARIES["aws_key"],
                    "ops.session": CANARIES["jwt"],
                    "ops.dsn": f"postgresql://shop:{CANARIES['db_password']}@db:5432/shop", "com.example.team": "storefront",
                    "owner.contact": f"{AI_ONLY['person']} (payroll team)",
                    "internal.docs": f"https://{AI_ONLY['internal_url']}/runbook",
                    "cloud.account": AI_ONLY["account"]},
            health=["CMD-SHELL", f"curl -u admin:{CANARIES['healthcheck_pw']} http://localhost:8080/health"],
            user="shop", workdir="/var/www/shop",
            mounts=[{"Type": "volume", "Destination": "/var/log/shop", "RW": True, "Source": CANARIES["host_path"]}],
            bindings={"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]},
        ),
        _container(
            "b" * 64, "shop-db", "db", "postgres:16-alpine",
            ["POSTGRES_USER=shop", f"POSTGRES_PASSWORD={CANARIES['db_password']}", "POSTGRES_DB=shop"], [5432],
        ),
        _container("c" * 64, "shop-cache", "cache", "redis:7-alpine", [], [6379]),
    ]


class FakeReadOnlyDocker:
    """Implements exactly the ReadOnlyDocker surface and records every call."""

    def __init__(self, containers=None, diff_paths=None):
        self.containers = containers if containers is not None else shop_containers()
        self.calls: list[str] = []
        self.diff_paths = diff_paths or ["/var/log/shop/app.log", CANARIES["fs_secret_path"]]

    def list_containers(self, project):
        self.calls.append("list_containers")
        return [c["summary"] for c in self.containers if c["summary"]["Labels"].get("com.docker.compose.project") == project]

    def inspect_container(self, container_id):
        self.calls.append("inspect_container")
        return next(c["inspect"] for c in self.containers if c["summary"]["Id"] == container_id)

    def container_diff(self, container_id):
        self.calls.append("container_diff")
        return [{"Path": p, "Kind": 0} for p in self.diff_paths]

    def list_network_names(self, project):
        self.calls.append("list_network_names")
        return ["shop-prod_shopnet"]


class FakeImageBuilder:
    def __init__(self, fail: bool = False):
        self.fail, self.built, self.removed = fail, [], []

    def build(self, context_dir: Path, tag: str) -> str:
        from app.environment.image_builder import ImageBuildError

        if self.fail:
            raise ImageBuildError("simulated build failure")
        h = hashlib.sha256()
        for f in sorted(Path(context_dir).rglob("*")):
            if f.is_file():
                h.update(f.read_bytes())
        image_id = "sha256:" + h.hexdigest()
        self.built.append((tag, image_id))
        return image_id

    def remove(self, image_id: str) -> None:
        self.removed.append(image_id)
