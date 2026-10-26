import copy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app import services
from app.ai.doc_validator import validate_documents
from app.ai.provider import TemplateProvider
from app.discovery.docker_inventory import collect
from app.environment import pipeline
from app.environment.builder import build_plan
from app.environment.graph_nodes import plan_nodes
from app.environment.image_builder import write_context
from app.environment.validator import validate_plan
from app.graph import NodeSpec, validate_tree
from app.models import AdminAuditEvent, EnvironmentSnapshot, ServiceProfile, TwinImage, VirtualNode
from app.sanitization.sanitizer import sanitize
from tests.helpers_discovery import ALL_CANARY_STRINGS, CANARIES, FakeImageBuilder, FakeReadOnlyDocker

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
ALLOWED = {"shop-prod"}


@pytest.fixture
def profile():
    return sanitize(collect(FakeReadOnlyDocker(), "shop-prod", ALLOWED))


@pytest.fixture
def plan(profile):
    return build_plan(profile, NOW, TemplateProvider(), ALL_CANARY_STRINGS)[0]


def _content(plan, path):
    return plan.files[path].content


# ---- builder -------------------------------------------------------------------------------------
def test_plan_is_deterministic(profile):
    a, _ = build_plan(profile, NOW, TemplateProvider())
    b, _ = build_plan(profile, NOW, TemplateProvider())
    assert {p: f.content for p, f in a.files.items()} == {p: f.content for p, f in b.files.items()}
    assert a.secrets == b.secrets


def test_different_environments_get_different_fake_secrets(profile):
    other = copy.deepcopy(profile)
    other.profile_hash = "f" * 64
    assert build_plan(profile, NOW)[0].secrets != build_plan(other, NOW)[0].secrets


def test_plan_covers_every_role_generically(plan):
    roles = {s.role for s in plan.services}
    assert roles == {"http-app", "postgres", "redis"}
    assert "/etc/postgresql/16/main/postgresql.conf" in plan.files
    assert "/etc/redis/redis.conf" in plan.files
    assert any(p.startswith("/var/www/shop/") for p in plan.files)  # workdir taken from the profile
    assert plan.databases and set(plan.databases[0].tables) == {"customers", "products", "orders"}


def test_credentials_are_consistent_across_resources(plan):
    db_pw, redis_pw = plan.secrets["db.password"], plan.secrets["cache.password"]
    env = _content(plan, "/var/www/shop/.env")
    assert db_pw in env and redis_pw in env
    assert db_pw in _content(plan, "/home/user/.pgpass")
    assert db_pw in _content(plan, "/docker-entrypoint-initdb.d/01-schema.sql")
    assert f"requirepass {redis_pw}" in _content(plan, "/etc/redis/redis.conf")
    for name in ("ADMIN_TOKEN", "STRIPE_API_KEY", "DATABASE_URL", "REDIS_URL", "LOG_FILE", "PORT"):
        assert f"{name}=" in env


def test_generated_data_uses_only_the_synthetic_world(plan):
    emails = {p.email for p in plan.people}
    assert all(e.endswith("@example.test") for e in emails)
    sql = _content(plan, "/docker-entrypoint-initdb.d/01-schema.sql")
    assert all(e in sql for e in emails)
    assert validate_plan(plan).ok


def test_no_real_canary_anywhere_in_the_plan(plan):
    blob = "\n".join((f.content or "") + f.path for f in plan.files.values())
    for canary in ALL_CANARY_STRINGS:
        assert canary not in blob


def test_users_files_and_passwd_agree(plan):
    passwd = _content(plan, "/etc/passwd")
    for u in plan.users.values():
        assert f"{u.name}:x:{u.uid}:" in passwd
    assert "user:x:1000:" in passwd and "hornet-sys:x:1001:" in passwd  # twin runtime accounts survive
    assert plan.files["/home/user/.pgpass"].mode == 0o600 and plan.files["/home/user/.pgpass"].uid == 1000


def test_graph_nodes_form_a_valid_tree(plan):
    nodes = plan_nodes(plan)
    assert validate_tree([NodeSpec(n.path, n.parent_path, n.path, n.node_type) for n in nodes]) == []
    types = {n.node_type for n in nodes}
    assert {"directory", "file", "service", "endpoint", "user", "database"} <= types
    assert any(n.path == "/services/web/tcp-8080" and n.parent_path == "/services/web" for n in nodes)
    assert all(n.content_ref is None or n.content_ref.startswith("/") for n in nodes)
    pgpass = next(n for n in nodes if n.path == "/home/user/.pgpass")
    assert pgpass.content_ref == "/usr/share/hornet/seed/home/user/.pgpass"  # tmpfs-backed paths are seeded


# ---- validator catches tampering -----------------------------------------------------------------
def _mutate(plan, fn):
    p = copy.deepcopy(plan)
    fn(p)
    return validate_plan(p)


@pytest.mark.parametrize(
    ("name", "mutation", "needle"),
    [
        ("user removed", lambda p: p.users.pop("postgres"), "postgres"),
        ("password drifts", lambda p: setattr(p.files["/var/www/shop/.env"], "content", "DATABASE_URL=x\n"), "not consistent"),
        ("real-looking secret", lambda p: p.add_file("/opt/x.txt", "key AKIAABCDEFGHIJKLMNOP\n"), "secret-like"),
        ("unknown email", lambda p: p.add_file("/opt/x.txt", "mail bob@real-corp.com\n"), "unknown email"),
        ("real IP", lambda p: p.add_file("/opt/x.txt", "host 8.8.8.8\n"), "non-synthetic IP"),
        ("unknown owner", lambda p: setattr(p.files["/etc/motd"], "uid", 4242), "not a known user"),
        ("service without log", lambda p: p.services[0].log_paths.clear(), "needs at least one"),
        ("dangling log ref", lambda p: p.services[0].log_paths.append("/var/log/nope.log"), "does not exist"),
        ("oversized file", lambda p: p.add_file("/opt/big.txt", "a" * 70_000), "too large"),
        ("empty table", lambda p: p.databases[0].tables.update(customers=[]), "empty table"),
    ],
)
def test_validator_catches(plan, name, mutation, needle):
    report = _mutate(plan, mutation)
    assert not report.ok and any(needle in e for e in report.errors), (name, report.errors)


def test_validator_flags_forbidden_values(plan):
    p = copy.deepcopy(plan)
    p.add_file("/opt/x.txt", "contains MARKER-123\n")
    assert any("forbidden" in e for e in validate_plan(p, ("MARKER-123",)).errors)


# ---- AI documents are validated in ordinary code ----------------------------------------------------
def test_template_provider_documents_are_accepted_and_labelled(profile):
    plan, rep = build_plan(profile, NOW, TemplateProvider())
    assert rep.accepted == 2 and not rep.rejected_reasons
    assert plan.files["/home/user/.bash_history"].origin == "ai:template"
    assert "psql -h db" in _content(plan, "/home/user/.bash_history")


class BadProvider:
    name, version = "bad", "0"

    def __init__(self, response=None, error=None):
        self.response, self.error = response, error

    def generate_structured(self, **_):
        if self.error:
            raise self.error
        return self.response


def _docs(*items):
    return {"documents": [{"path": p, "content": c, "purpose": "x"} for p, c in items]}


@pytest.mark.parametrize(
    ("path", "content", "reason"),
    [
        ("/home/user/../../etc/cron.d/x", "ok", "path not allowed"),
        ("/etc/passwd", "root:x:0:0::/root:/bin/bash\n", "path not allowed"),
        ("/usr/share/hornet/seed/x", "ok", "path not allowed"),
        ("/var/www/shop/.env", "PORT=1\n", "would overwrite"),
        ("/opt/n.txt", "password=Sup3rS3cretValue", "secret-like"),
        ("/opt/n.txt", "ssh to 8.8.8.8", "non-synthetic IP"),
        ("/opt/n.txt", "mail ceo@real-company.com", "unknown email"),
        pytest.param("/opt/n.txt", "x" * 70_000, "too large", id="oversized"),
        ("/opt/n.txt", "bad \x00 byte", "too large or binary"),
        ("/opt/n.txt", f"secret is {CANARIES['db_password']}", "forbidden"),
    ],
)
def test_adversarial_documents_are_rejected(plan, path, content, reason):
    result = validate_documents(_docs((path, content)), plan, ALL_CANARY_STRINGS)
    assert result.accepted == []
    assert any(reason in r for r in result.rejected), result.rejected
    assert content not in " ".join(result.rejected)  # reasons never echo rejected content


def test_invalid_responses_never_fail_a_build(profile):
    for provider in (
        BadProvider(error=TimeoutError()),
        BadProvider(response="not json"),
        BadProvider(response={"documents": "nope"}),
        BadProvider(response=_docs(("/etc/passwd", "evil"))),
    ):
        plan, rep = build_plan(profile, NOW, provider)
        assert rep.accepted == 0
        assert validate_plan(plan).ok  # the plan is complete without AI content


def test_valid_document_is_accepted(plan):
    ok = _docs(("/opt/web/RUNBOOK.md", f"Restart the service. Escalate to {plan.people[0].email}"))
    result = validate_documents(ok, plan)
    assert [d.path for d in result.accepted] == ["/opt/web/RUNBOOK.md"] and result.rejected == []


# ---- build context ---------------------------------------------------------------------------------
def test_write_context_layout(plan, tmp_path):
    digest = write_context(plan, tmp_path / "ctx", "hornet-twin-base:2", "ab" * 32)
    ctx = tmp_path / "ctx"
    assert (ctx / "rootfs/etc/passwd").read_text().startswith("root:x:0:0")
    assert (ctx / "rootfs/usr/share/hornet/seed/home/user/.pgpass").exists()
    assert not (ctx / "rootfs/home/user/.pgpass").exists()
    manifest = (ctx / "manifest.tsv").read_text().splitlines()
    assert "0600\t1000\t1000\t/usr/share/hornet/seed/home/user/.pgpass" in manifest
    assert not any(line.endswith("\t/etc") for line in manifest)  # base dirs are left alone
    docker = (ctx / "Dockerfile").read_text()
    assert docker.startswith("FROM hornet-twin-base:2") and "USER 1001:1001" in docker
    assert b"\r" not in (ctx / "manifest.tsv").read_bytes()
    assert digest == write_context(plan, tmp_path / "ctx2", "hornet-twin-base:2", "ab" * 32)


# ---- pipeline ---------------------------------------------------------------------------------------
def _build(db, tmp_path, builder=None, docker=None, project="shop-prod", allowed=ALLOWED):
    builder = builder or FakeImageBuilder()
    snap = pipeline.build_from_docker(
        db, docker=docker or FakeReadOnlyDocker(), image_builder=builder, provider=TemplateProvider(),
        project=project, allowed_projects=allowed, base_image="hornet-twin-base:2", build_dir=tmp_path,
        actor="tester", now=NOW, forbidden=ALL_CANARY_STRINGS,
    )
    return snap, builder


def _db_dump(db) -> str:
    out = []
    for table in db.get_bind().dialect.get_table_names(db.connection()):
        for row in db.execute(text(f'SELECT * FROM "{table}"')).fetchall():
            out.append(repr(row))
    return "\n".join(out)


def test_pipeline_produces_validated_draft_snapshot(db, tmp_path):
    snap, builder = _build(db, tmp_path)
    assert snap.status == "validated" and snap.version == 1
    assert snap.validation_report["ok"] and snap.validation_report["build"]["stages"][-1] == "image_built"
    assert snap.validation_report["build"]["ai"]["accepted"] == 2
    image = db.scalars(select(TwinImage).where(TwinImage.snapshot_id == snap.id)).one()
    assert image.image_digest == builder.built[0][1] and image.runtime_type == "docker"
    assert {s.service_type for s in db.scalars(select(ServiceProfile))} == {"http-app", "postgres", "redis"}
    nodes = db.scalars(select(VirtualNode).where(VirtualNode.snapshot_id == snap.id)).all()
    assert len(nodes) > 50 and next(n for n in nodes if n.virtual_path == "/").parent_id is None
    # file CONTENTS never go into the database, only references and hashes
    assert all(n.content_ref is None or n.content_ref.startswith("/") for n in nodes)
    assert "psql -h db" not in _db_dump(db)


def test_no_canary_reaches_database_context_or_audit(db, tmp_path):
    snap, _ = _build(db, tmp_path)
    haystacks = [_db_dump(db)]
    for f in Path(tmp_path).rglob("*"):
        if f.is_file():
            haystacks.append(f.read_text(errors="ignore") + f.name)
    blob = "\n".join(haystacks)
    for name, canary in CANARIES.items():
        assert canary not in blob, f"canary {name} leaked"
    assert db.scalars(select(AdminAuditEvent).where(AdminAuditEvent.action == "environment.build")).one().detail["project"] == "shop-prod"


def test_built_snapshot_is_publishable_then_immutable(db, tmp_path):
    from app.immutability import ImmutableError

    snap, _ = _build(db, tmp_path)
    services.publish_snapshot(db, snap)
    db.commit()
    node = db.scalars(select(VirtualNode).where(VirtualNode.snapshot_id == snap.id)).first()
    node.permissions = "0777"
    with pytest.raises(ImmutableError):
        db.commit()
    db.rollback()


def test_revalidation_keeps_the_build_report(db, tmp_path):
    snap, _ = _build(db, tmp_path)
    services.validate_snapshot(db, snap)
    assert "build" in snap.validation_report


def test_second_build_is_a_new_version_and_never_replaces_the_published_one(db, tmp_path):
    first, _ = _build(db, tmp_path)
    services.publish_snapshot(db, first)
    db.commit()
    second, _ = _build(db, tmp_path / "again")
    assert second.version == 2 and second.status == "validated"
    db.refresh(first)
    assert first.status == "published"


def test_every_build_gets_its_own_image_tag(db, tmp_path):
    """Regression (found while rehearsing the demo): every build of the same server used the same tag, so a rebuild
    took the tag from the published environment's image, which `docker image prune` would then delete."""
    builder = FakeImageBuilder()
    _build(db, tmp_path, builder=builder)
    _build(db, tmp_path / "again", builder=builder)
    (tag1, _), (tag2, _) = builder.built
    assert tag1 != tag2
    assert all(t.startswith("hornet-twin:") and t.split(":", 1)[1].startswith(t.split(":", 1)[1][:12]) for t in (tag1, tag2))
    assert tag1.split(":", 1)[1][:12] == tag2.split(":", 1)[1][:12]  # same server, same readable prefix


def test_unlisted_project_is_refused_and_builds_nothing(db, tmp_path):
    from app.discovery.docker_inventory import DiscoveryNotAllowed

    builder = FakeImageBuilder()
    with pytest.raises(DiscoveryNotAllowed):
        _build(db, tmp_path, builder=builder, allowed=set())
    assert builder.built == [] and db.scalars(select(EnvironmentSnapshot)).all() == []


def test_sanitization_failure_rejects_the_build_and_alerts(db, tmp_path, monkeypatch):
    from app.sanitization import sanitizer as sz

    monkeypatch.setattr(pipeline, "sanitize", lambda raw: (_ for _ in ()).throw(sz.SanitizationFailure("leak")))
    builder = FakeImageBuilder()
    with pytest.raises(pipeline.BuildFailed) as exc:
        _build(db, tmp_path, builder=builder)
    assert exc.value.stage == "sanitization" and builder.built == []
    actions = {a.action for a in db.scalars(select(AdminAuditEvent))}
    assert {"environment.build_failed", "security.sanitization_failed"} <= actions
    assert db.scalars(select(EnvironmentSnapshot)).all() == []


def test_forbidden_value_in_profile_rejects_the_build(db, tmp_path):
    with pytest.raises(pipeline.BuildFailed) as exc:
        pipeline.build_from_docker(
            db, docker=FakeReadOnlyDocker(), image_builder=FakeImageBuilder(), provider=None, project="shop-prod",
            allowed_projects=ALLOWED, base_image="b", build_dir=tmp_path, actor="t", now=NOW, forbidden=("storefront",),
        )
    assert exc.value.stage == "sanitization"


def test_validation_failure_stops_before_any_image_is_built(db, tmp_path, monkeypatch):
    from app.environment.validator import ValidationReport

    monkeypatch.setattr(pipeline, "validate_plan", lambda plan, forbidden=(): ValidationReport(errors=["boom"]))
    builder = FakeImageBuilder()
    with pytest.raises(pipeline.BuildFailed) as exc:
        _build(db, tmp_path, builder=builder)
    assert exc.value.stage == "validation" and builder.built == []


def test_image_build_failure_leaves_nothing_behind(db, tmp_path):
    with pytest.raises(pipeline.BuildFailed) as exc:
        _build(db, tmp_path, builder=FakeImageBuilder(fail=True))
    assert exc.value.stage == "image"
    assert db.scalars(select(EnvironmentSnapshot)).all() == [] and db.scalars(select(TwinImage)).all() == []


def test_database_failure_after_image_build_removes_the_image(db, tmp_path, monkeypatch):
    builder = FakeImageBuilder()
    monkeypatch.setattr(pipeline, "_materialize", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    with pytest.raises(pipeline.BuildFailed):
        _build(db, tmp_path, builder=builder)
    assert builder.removed == [builder.built[0][1]]
    assert db.scalars(select(EnvironmentSnapshot)).all() == []
