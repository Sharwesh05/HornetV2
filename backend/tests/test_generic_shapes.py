"""The pipeline must work for ANY observed server, not just the demo. These tests feed it servers of very
different shapes and check that a valid, leak-free environment always comes out the other side."""
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.environment import data_model as dm
from app.environment.builder import build_plan
from app.environment.plan import Person
from app.environment.role_detect import detect_role
from app.environment.synthetic_data import Synth
from app.environment.validator import validate_plan
from app.profile import RawEnvironment, RawMount, RawPort, RawService
from app.sanitization.sanitizer import sanitize
from tests.helpers_discovery import FakeReadOnlyDocker

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def svc(name, image, ports=(), env=(), cmd=(), user=None, workdir=None, mounts=(), labels=None, published=False):
    return RawService(
        name=name, container_name=name, image=image, env_names=list(env), command=list(cmd), user=user,
        workdir=workdir, labels=labels or {}, mounts=[RawMount("volume", m) for m in mounts],
        ports=[RawPort(p, "tcp", "127.0.0.1" if published else None, p if published else None) for p in ports],
    )


SHAPES = {
    "single generic service, nothing else": [svc("thing", "vendor/thing:2")],
    "two postgres and two redis": [
        svc("pg-a", "postgres:16", [5432], ["POSTGRES_PASSWORD"]), svc("pg-b", "postgres:15", [5432], ["POSTGRES_PASSWORD"]),
        svc("cache-a", "redis:7", [6379]), svc("cache-b", "redis:6", [6379]),
        svc("web", "myorg/web:1", [8080], ["DATABASE_URL", "REDIS_URL", "SECRET_KEY"]),
    ],
    "mysql + node app + nginx": [
        svc("db", "mariadb:11", [3306], ["MYSQL_ROOT_PASSWORD"]),
        svc("app", "node:20", [3000], ["DB_URL", "JWT_SECRET"], cmd=["npm", "start"], workdir="/usr/src/app", user="node"),
        svc("proxy", "nginx:1.27", [80, 443]),
    ],
    "java app with no database": [svc("gateway", "eclipse-temurin:21", [8081], cmd=["java", "-jar", "app.jar"], published=True)],
    "only infrastructure, no app": [svc("proxy", "traefik:3", [80]), svc("cache", "valkey/valkey:8", [6379])],
    "awkward names and paths": [
        svc("Web_Frontend.v2", "registry.corp.local:5000/team/web:1", [8080], ["APP_SECRET"], workdir="C:\\app\\here"),
        svc("api.internal", "python:3.12", [9000], cmd=["python", "-m", "http.server"], user="1000", workdir="/",
            mounts=["/var/log/../../etc", "/data/ünïcode"]),
    ],
    "no env, no ports, no command": [svc("a", "x"), svc("b", "y"), svc("c", "z")],
    "service on an unusual published port": [svc("portal", "acme/portal:9", [7777], published=True)],
    "twenty-five mixed services": (
        [svc(f"w{i}", "acme/web:1", [8000 + i], [f"K{i}_TOKEN"]) for i in range(10)]
        + [svc(f"r{i}", "redis:7", [6379]) for i in range(5)] + [svc(f"p{i}", "postgres:16", [5432]) for i in range(5)]
        + [svc(f"g{i}", "misc/tool", []) for i in range(5)]
    ),
}


@pytest.mark.parametrize("name", list(SHAPES))
def test_any_server_shape_yields_a_valid_leak_free_environment(name):
    profile = sanitize(RawEnvironment(project="any-project", services=SHAPES[name]))
    plan, _ = build_plan(profile, NOW)
    report = validate_plan(plan)
    assert report.ok, report.errors[:5]
    assert len(plan.services) == len(SHAPES[name])
    for sp in plan.services:  # every service gets a user, config, log and a node in the graph
        assert sp.user in plan.users and sp.config_paths and sp.log_paths
    assert all(" " not in p and ":" not in p and "<" not in p for p in plan.files)  # image-path safe
    # building twice from the same profile gives the same environment
    again, _ = build_plan(profile, NOW)
    assert {p: f.content for p, f in again.files.items()} == {p: f.content for p, f in plan.files.items()}


def test_services_of_the_same_kind_do_not_overwrite_each_other():
    profile = sanitize(RawEnvironment(project="multi", services=SHAPES["two postgres and two redis"]))
    plan, _ = build_plan(profile, NOW)
    redis_confs = [p for p in plan.files if p.startswith("/etc/redis/")]
    assert len(redis_confs) == 2
    assert len({d.name for d in plan.databases}) == 2
    pgpass = plan.files["/home/user/.pgpass"].content
    assert pgpass.count("\n") == 2 and all(plan.secrets[f"{s}.password"] in pgpass for s in ("pg-a", "pg-b"))


def test_unusual_profile_values_never_become_real_paths():
    profile = sanitize(RawEnvironment(project="p", services=SHAPES["awkward names and paths"]))
    plan, _ = build_plan(profile, NOW)
    assert not any(".." in p.split("/") for p in plan.files)
    assert "/var/www/web_frontend.v2" in {p.rsplit("/", 1)[0] for p in plan.files if p.endswith("/.env")}
    assert any(p.startswith("/var/log/api.internal/") for p in plan.files)  # bad mount ignored, safe default used


def test_runtime_stub_follows_the_hints_in_the_profile():
    node = sanitize(RawEnvironment(project="p", services=SHAPES["mysql + node app + nginx"]))
    plan, _ = build_plan(node, NOW)
    pkg = json.loads(plan.files["/usr/src/app/package.json"].content)
    assert pkg["dependencies"] == {"mysql2": "^3.9.0"} and "/usr/src/app/src/index.js" in plan.files
    java = sanitize(RawEnvironment(project="p", services=SHAPES["java app with no database"]))
    plan, _ = build_plan(java, NOW)
    assert any(p.endswith("application.yml") for p in plan.files) and not any(p.endswith(".py") for p in plan.files)
    py = sanitize(RawEnvironment(project="p", services=[svc("api", "python:3.12", [9000], cmd=["gunicorn", "x:y"])]))
    assert any(p.endswith("app/main.py") for p in build_plan(py, NOW)[0].files)


def test_no_data_model_when_nothing_stores_data():
    profile = sanitize(RawEnvironment(project="p", services=SHAPES["only infrastructure, no app"]))
    plan, _ = build_plan(profile, NOW)
    assert plan.notes["data_model"]["source"] == "none" and not plan.databases


@pytest.mark.parametrize(
    ("image", "ports", "cmd", "published", "role"),
    [
        ("python:3.12-slim", [9000], "python -m http.server 9000", True, "http-app"),
        ("acme/portal:9", [7777], "", True, "http-app"),       # unusual published port
        ("acme/portal:9", [5672], "", True, "generic"),        # a message broker, not a web app
        ("eclipse-temurin:21", [8081], "java -jar app.jar", False, "generic"),
        ("acme/web", [], "node server.js", False, "http-app"),  # command says so
        ("worker/img", [], "celery worker", False, "generic"),
        ("mariadb:11", [3306], "", True, "mysql"),
    ],
)
def test_role_detection_is_not_tied_to_one_port_list(image, ports, cmd, published, role):
    assert detect_role(image, ports, cmd, published) == role


# ---- data model ------------------------------------------------------------------------------------
def _model(*tables):
    return {"tables": [{"name": n, "columns": [{"name": c, "type": t, **({"ref": r} if r else {})} for c, t, r in cols]}
                       for n, cols in tables]}


GOOD = _model(("devices", [("id", "id", None), ("owner_email", "email", None), ("label", "text", None)]),
              ("readings", [("id", "id", None), ("device_id", "ref", "devices"), ("value", "amount", None)]))


@pytest.mark.parametrize(
    ("what", "raw"),
    [
        ("reserved table name", _model(("user", [("id", "id", None), ("x", "int", None)]))),
        ("bad identifier", _model(("Bad-Name", [("id", "id", None), ("x", "int", None)]))),
        ("no id first", _model(("t", [("x", "int", None), ("id", "id", None)]))),
        ("unknown type", _model(("t", [("id", "id", None), ("x", "blob", None)]))),
        ("ref to nowhere", _model(("t", [("id", "id", None), ("x", "ref", "ghost")]))),
        ("ref without target", _model(("t", [("id", "id", None), ("x", "ref", None)]))),
        ("target without ref type", _model(("t", [("id", "id", None), ("x", "int", "t")]))),
        ("circular refs", _model(("a", [("id", "id", None), ("b_id", "ref", "b")]), ("b", [("id", "id", None), ("a_id", "ref", "a")]))),
        ("self reference", _model(("a", [("id", "id", None), ("p", "ref", "a")]))),
        ("duplicate tables", _model(("a", [("id", "id", None), ("x", "int", None)]), ("a", [("id", "id", None), ("x", "int", None)]))),
        ("too many tables", {"tables": [_model((f"t{i}", [("id", "id", None), ("x", "int", None)]))["tables"][0] for i in range(7)]}),
        ("one column", _model(("t", [("id", "id", None)]))),
        ("empty", {"tables": []}),
    ],
)
def test_invalid_data_models_are_rejected(what, raw):
    with pytest.raises(ValidationError):
        dm.DataModel.model_validate(raw)


def test_rows_are_consistent_deterministic_and_use_only_fictional_people():
    people = [Person("Ana", "Lee", "ana.lee@example.test"), Person("Bo", "Kim", "bo.kim@example.test")]
    model = dm.DataModel.model_validate(GOOD)
    a = dm.generate_rows(model, Synth(1, NOW), people)
    b = dm.generate_rows(model, Synth(1, NOW), people)
    assert a == b and set(a) == {"devices", "readings"}
    assert {r["owner_email"] for r in a["devices"]} <= {p.email for p in people}
    assert {r["device_id"] for r in a["readings"]} <= {r["id"] for r in a["devices"]}  # foreign keys resolve
    assert dm.generate_rows(model, Synth(2, NOW), people) != a


@pytest.mark.parametrize("engine", ["postgres", "mysql"])
def test_sql_covers_every_table_in_dependency_order(engine):
    model = dm.DataModel.model_validate(GOOD)
    rows = dm.generate_rows(model, Synth(1, NOW), [Person("Ana", "Lee", "ana.lee@example.test")])
    ddl, ins = dm.schema_sql(model, engine), dm.inserts_sql(model, rows, engine)
    assert ddl.index("CREATE TABLE devices") < ddl.index("CREATE TABLE readings")
    assert ins.count("INSERT INTO devices") == len(rows["devices"]) and "INSERT INTO readings" in ins


@pytest.mark.parametrize(
    ("project", "image", "domain"),
    [("shop-prod", "x", "commerce"), ("helpdesk-staging", "x", "helpdesk"), ("p", "ghost:5", "content"),
     ("telemetry-core", "influx", "generic")],
)
def test_keyword_domain_defaults(project, image, domain):
    profile = sanitize(RawEnvironment(project=project, services=[svc("web", image, [8080])]))
    assert dm.keyword_domain(profile) == domain
    assert dm.pick_model(profile, None)[1]["source"] == f"keyword:{domain}"


class Model:
    name, version, is_model = "m", "1", True

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.contexts = answer, error, []

    def generate_structured(self, *, system_policy, context, output_schema):
        self.contexts.append(context)
        if self.error:
            raise self.error
        return self.answer


def _profile():
    return sanitize(RawEnvironment(project="telemetry-core", services=[svc("web", "acme/web", [8080]), svc("db", "postgres:16", [5432])]))


def test_ai_proposed_schema_is_used_when_valid():
    model, info = dm.pick_model(_profile(), Model(GOOD))
    assert info["source"] == "ai:m" and [t.name for t in model.tables] == ["devices", "readings"]


@pytest.mark.parametrize("answer", [{}, {"tables": "x"}, _model(("user", [("id", "id", None), ("x", "int", None)])), "garbage"])
def test_invalid_ai_schema_falls_back_to_a_default(answer):
    model, info = dm.pick_model(_profile(), Model(answer))
    assert info["source"] == "keyword:generic" and "ai_rejected" in info and model.domain == "generic"


def test_ai_outage_falls_back_without_failing():
    model, info = dm.pick_model(_profile(), Model(error=TimeoutError()))
    assert info["source"] == "keyword:generic" and info["ai_error"] == "TimeoutError"


def test_model_receives_only_sanitized_structure():
    m = Model(GOOD)
    dm.pick_model(sanitize(RawEnvironment(project="p", services=[svc("web", "acme/web", [8080], ["DB_PASSWORD"])])), m)
    sent = json.dumps(m.contexts)
    assert "web" in sent and "DB_PASSWORD" not in sent and "env" not in sent


def test_environment_built_around_an_ai_schema_validates():
    profile = _profile()
    plan, _ = build_plan(profile, NOW, Model(GOOD))
    assert plan.notes["data_model"]["source"] == "ai:m"
    assert set(plan.databases[0].tables) == {"devices", "readings"}
    assert "/api/devices" in "".join(f.content or "" for f in plan.files.values())
    assert validate_plan(plan).ok


def test_the_demo_is_just_one_input_among_many():
    """Nothing in the builder output may mention the demo stack unless the profile itself does."""
    profile = sanitize(RawEnvironment(project="telemetry-core", services=SHAPES["java app with no database"]))
    plan, _ = build_plan(profile, NOW)
    blob = " ".join((f.content or "") + p for p, f in plan.files.items()).lower()
    for demo_word in ("shop", "customers", "products", "orders", "stripe", "flask", "sku"):
        assert demo_word not in blob, demo_word
    assert FakeReadOnlyDocker().containers  # (the demo fixtures are only used by demo-specific tests)
