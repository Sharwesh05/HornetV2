"""Phase 3 exit criteria against real Docker, run against TWO deliberately different servers.

Every assertion is derived from what discovery found, never from knowledge of a particular demo: the
same test body must pass for the shop stack and for the helpdesk stack (different project, service
names, images, ports, env vars, mounts and business domain). Skipped per stack when it is not running.

Start the stacks with:
  docker compose -f main-server/docker-compose.yml up -d
  docker compose -f demo-servers/helpdesk/docker-compose.yml up -d
"""
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from app import services
from app.ai.provider import TemplateProvider
from app.discovery.docker_inventory import ReadOnlyDocker, collect
from app.environment import pipeline
from app.models import AdminAuditEvent, TwinImage, VirtualNode
from app.orchestration import session_manager as sm
from app.runtime.policy import ExecutionPolicy

BASE = "hornet-twin-base:2"

STACKS = {
    "shop-prod": {
        "containers": ("shop-web", "shop-db", "shop-cache"),
        "canaries": ("CANARY-DB-PASSWORD-7f3a", "CANARY-ADMIN-TOKEN-91c2", "sk_test_CANARY0000000000000000"),
        "env_canaries": ("CANARY-DB-PASSWORD-7f3a", "CANARY-ADMIN-TOKEN-91c2", "sk_test_CANARY0000000000000000"),
        "roles": {"http-app", "postgres", "redis"},
        "domain": "commerce",
    },
    "helpdesk-staging": {
        "containers": ("hd-api", "hd-worker", "hd-pg", "hd-broker"),
        "canaries": ("CANARY-HD-SECRET-KEY-aa11", "CANARY-HD-SMTP-PASS-bb22", "CANARYsentrykey33cc",
                     "CANARY-HD-OIDC-SECRET-dd44", "CANARY-HD-PG-PASS-ee55", "priya.raman@hd-canary.example"),
        # values that only ever appeared in environment variables (the maintainer e-mail is a label, which
        # discovery legitimately reads before the sanitizer redacts it)
        "env_canaries": ("CANARY-HD-SECRET-KEY-aa11", "CANARY-HD-SMTP-PASS-bb22", "CANARYsentrykey33cc",
                         "CANARY-HD-OIDC-SECRET-dd44", "CANARY-HD-PG-PASS-ee55"),
        "roles": {"http-app", "generic", "postgres", "redis"},
        "domain": "helpdesk",
    },
}


def _docker():
    try:
        import docker

        c = docker.from_env()
        c.ping()
        c.images.get(BASE)
        return c
    except Exception:
        return None


def _running(project: str) -> bool:
    c = _docker()
    if c is None:
        return False
    names = {x.name for x in c.containers.list(filters={"label": f"com.docker.compose.project={project}"})}
    return set(STACKS[project]["containers"]) <= names


def _param(project):
    return pytest.param(project, id=project, marks=pytest.mark.skipif(
        not _running(project), reason=f"{project} stack (or Docker / {BASE}) is not running"))


pytestmark = pytest.mark.docker
PROJECTS = [_param(p) for p in STACKS]


@pytest.fixture(scope="module")
def client():
    return _docker()


class ApiSpy:
    """Records every Docker API call made through it."""

    def __init__(self, api):
        self._api, self.calls = api, []

    def __getattr__(self, name):
        target = getattr(self._api, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            self.calls.append(name)
            return target(*args, **kwargs)

        return call


@pytest.mark.parametrize("project", PROJECTS)
def test_discovery_is_read_only_and_leaks_nothing(client, project):
    """Docker's own HEALTHCHECKs exec inside containers on their own schedule, so exec counts are not a reliable
    signal. Instead record every API call discovery makes: only the four read calls may appear."""
    from types import SimpleNamespace

    cfg = STACKS[project]
    started = {n: client.containers.get(n).attrs["State"]["StartedAt"] for n in cfg["containers"]}
    spy = ApiSpy(client.api)

    raw = collect(ReadOnlyDocker(SimpleNamespace(api=spy)), project, {project})
    assert len(raw.services) == len(cfg["containers"])
    assert set(spy.calls) <= {"containers", "inspect_container", "diff", "networks"}, spy.calls
    dumped = json.dumps([s.__dict__ for s in raw.services], default=lambda o: o.__dict__)
    for canary in cfg["env_canaries"]:  # environment values were dropped at the source: names only
        assert canary not in dumped

    assert {n: client.containers.get(n).attrs["State"]["StartedAt"] for n in cfg["containers"]} == started  # nothing restarted


@pytest.mark.parametrize("project", PROJECTS)
def test_full_build_publish_and_twin(db, session_factory, client, tmp_path, project):
    from app.environment.image_builder import DockerImageBuilder

    cfg = STACKS[project]
    builder = DockerImageBuilder(client)
    snap = pipeline.build_from_docker(
        db, docker=ReadOnlyDocker(client), image_builder=builder, provider=TemplateProvider(), project=project,
        allowed_projects={project}, base_image=BASE, build_dir=tmp_path, actor="itest", now=datetime.now(UTC),
        forbidden=cfg["canaries"],
    )
    image = db.scalars(select(TwinImage).where(TwinImage.snapshot_id == snap.id)).one()
    try:
        # ---- the snapshot reflects what was discovered ------------------------------------------------
        assert snap.status == "validated" and snap.validation_report["ok"]
        build = snap.validation_report["build"]
        assert build["profile"]["source"] == project
        assert {s["role"] for s in build["profile"]["services"]} == cfg["roles"]
        assert build["data_model"]["domain"] == cfg["domain"]
        client.images.get(image.image_digest)  # the pinned image really exists

        nodes = db.scalars(select(VirtualNode).where(VirtualNode.snapshot_id == snap.id)).all()
        svc_nodes = [n for n in nodes if n.node_type == "service"]
        assert {n.virtual_path.rsplit("/", 1)[1] for n in svc_nodes} == {s["name"] for s in build["profile"]["services"]}

        # ---- the canaries never left the production stack -------------------------------------------------
        blob = []
        for table in db.get_bind().dialect.get_table_names(db.connection()):
            blob += [repr(r) for r in db.execute(text(f'SELECT * FROM "{table}"')).fetchall()]
        blob += [p.read_text(errors="ignore") for p in tmp_path.rglob("*") if p.is_file()]
        for canary in cfg["canaries"]:
            assert canary not in "\n".join(blob), f"{canary} leaked"
        pattern = " ".join(f"-e {c}" for c in cfg["canaries"])
        scan = client.containers.run(
            image.image_digest, ["sh", "-c", f"grep -rIl {pattern} / --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev "
                                 "2>/dev/null | head -5; echo scan-finished"],
            entrypoint="", detach=True, network_mode="none", user="0:0", cap_drop=["ALL"], read_only=True,
        )
        try:
            scan.wait(timeout=120)
            out = scan.logs()
        finally:
            scan.remove(force=True)
        assert out.decode().strip() == "scan-finished", "a canary string exists inside the built image"

        # ---- publish, then run a real twin from the snapshot's image ---------------------------------------
        services.publish_snapshot(db, snap)
        db.commit()
        from app.runtime.docker_driver import HardenedDockerDriver

        mgr = sm.SessionManager(HardenedDockerDriver(client), session_factory, ExecutionPolicy(timeout_ms=5000), BASE)
        sess = mgr.create_session()
        try:
            def sh(cmd):
                return mgr.execute(sess.id, cmd)

            passwd = sh("cat /etc/passwd").stdout
            for n in (n for n in nodes if n.node_type == "user"):                # every planned account exists
                assert f"\n{n.virtual_path.rsplit('/', 1)[1]}:x:" in "\n" + passwd
            for n in svc_nodes:                                                  # every planned config/log exists
                for path in n.meta["config_paths"] + n.meta["log_paths"]:
                    if path.endswith("/.env"):
                        denied = sh(f"cat {path}")                               # real permissions apply to the attacker
                        assert denied.exit_code != 0 and "Permission denied" in denied.stderr
                    else:
                        assert sh(f"test -s {path}").exit_code == 0, path
            if "postgres" in cfg["roles"]:
                pgpass = sh("cat ~/.pgpass")                                     # bait seeded into the attacker's home
                assert pgpass.exit_code == 0 and ":5432:" in pgpass.stdout
                assert sh("stat -c %a ~/.pgpass").stdout.strip() == "600"
            assert sh("test -s ~/.bash_history").exit_code == 0                  # template-provider document
            for canary in cfg["canaries"]:
                assert sh(f"grep -rIl {canary} / 2>/dev/null | head -1").stdout.strip() == ""

            # seeding must not look like attacker activity, but real changes still do
            assert sh("true").fs_changes == []
            assert [c["path"] for c in sh("echo x > /tmp/mark").fs_changes] == ["/tmp/mark"]
        finally:
            mgr.destroy_session(sess.id)
            mgr.reconcile()
    finally:
        builder.remove(image.image_digest)

    assert "environment.build" in {a.action for a in db.scalars(select(AdminAuditEvent))}
