import json

import pytest

from app.discovery.docker_inventory import (
    DiscoveryError,
    DiscoveryNotAllowed,
    ReadOnlyDocker,
    collect,
)
from app.environment.role_detect import detect_role
from app.profile import ServiceSpec
from app.sanitization.classifier import classify_env_name
from app.sanitization.patterns import find_secrets, redact
from app.sanitization.sanitizer import SanitizationFailure, assert_clean, sanitize
from tests.helpers_discovery import ALL_CANARY_STRINGS, CANARIES, FakeReadOnlyDocker, shop_containers

ALLOWED = {"shop-prod"}


# ---- pattern detection ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("postgresql://shop:hunter2pass@db:5432/shop", "url_credentials"),
        ("AKIAABCDEFGHIJKLMNOP", "aws_access_key"),
        ("sk_live_abcdefgh12345678", "stripe_key"),
        ("ghp_abcdefghijklmnopqrstuvwxyz0123", "github_token"),
        ("xoxb-1234567890-abcdefghij", "slack_token"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4eHh4In0.c2lnbmF0dXJlMTIz", "jwt"),
        ("Authorization: Bearer abcdef0123456789abcdef", "bearer_token"),
        ("password=hunter2pass", "kv_secret"),
        ('{"api_key": "abcd1234efgh"}', "kv_secret"),
        ("run --password hunter2pass now", "cli_secret"),
        ("contact ops@corp.example now", "email"),
        ("connect to 172.18.0.7 please", "ipv4"),
        ("C:\\Users\\bob\\secret\\app", "host_path"),
        ("/home/bob/.ssh/id_rsa", "host_path"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private_key"),
        ("token Zk3Jd9Qw7LmXp2Rv5TnB8cY4aHu6EsGf1", "high_entropy"),
    ],
)
def test_detects_each_kind(text, kind):
    assert kind in {f.kind for f in find_secrets(text)}


@pytest.mark.parametrize(
    "benign",
    [
        "postgres:16-alpine", "redis:7-alpine", "8080/tcp", "SKU-1001", "127.0.0.1", "0.0.0.0",
        "sha256:" + "ab12" * 16, "/var/log/postgresql/postgresql-16-main.log", "gunicorn --bind 0.0.0.0:8080 app.server:app",
        "123e4567-e89b-12d3-a456-426614174000", "python:3.12-slim", "CMD-SHELL pg_isready -U shop -d shop",
        "password: <REDACTED:kv_secret>",
    ],
)
def test_benign_strings_are_not_flagged(benign):
    assert find_secrets(benign) == []


def test_redact_removes_values_and_is_idempotent():
    text = f"url=postgresql://u:{CANARIES['db_password']}@db/x token={CANARIES['admin_token']} a@b.example"
    out, findings = redact(text)
    assert findings and CANARIES["db_password"] not in out and CANARIES["admin_token"] not in out
    assert find_secrets(out) == []
    assert redact(out)[0] == out


@pytest.mark.parametrize(
    ("name", "cls"),
    [("ADMIN_TOKEN", "secret"), ("POSTGRES_PASSWORD", "secret"), ("STRIPE_API_KEY", "secret"),
     ("DATABASE_URL", "connection"), ("REDIS_URL", "connection"), ("PORT", "config"), ("LOG_FILE", "config")],
)
def test_env_name_classification(name, cls):
    assert classify_env_name(name) == cls


@pytest.mark.parametrize(
    ("image", "ports", "role"),
    [("postgres:16-alpine", [5432], "postgres"), ("mariadb:11", [3306], "mysql"), ("redis:7", [6379], "redis"),
     ("nginx:1.27", [80], "nginx"), ("shop-web:1.0", [8080], "http-app"), ("custom/thing:2", [9999], "generic"),
     ("library/postgres:15", [], "postgres")],
)
def test_role_detection(image, ports, role):
    assert detect_role(image, ports) == role


# ---- discovery is read-only and allowlisted --------------------------------------------------------
def test_read_only_wrapper_exposes_only_reads():
    public = {n for n in dir(ReadOnlyDocker) if not n.startswith("_")}
    assert public == {"list_containers", "inspect_container", "container_diff", "list_network_names"}

    class Client:
        api = object()

    wrapped = ReadOnlyDocker(Client())
    for forbidden in ("api", "client", "exec_create", "exec_start", "kill", "remove_container", "start", "stop",
                      "create_container", "get_archive", "put_archive", "restart"):
        assert not hasattr(wrapped, forbidden)


def test_collect_only_uses_read_calls():
    fake = FakeReadOnlyDocker()
    collect(fake, "shop-prod", ALLOWED)
    assert set(fake.calls) <= {"list_containers", "inspect_container", "container_diff", "list_network_names"}


@pytest.mark.parametrize("project", ["other", "shop", "SHOP-PROD", "", "../etc", "shop-prod; rm -rf /"])
def test_project_must_be_allowlisted(project):
    with pytest.raises(DiscoveryError):
        collect(FakeReadOnlyDocker(), project, ALLOWED)


def test_empty_allowlist_denies_everything():
    with pytest.raises(DiscoveryNotAllowed):
        collect(FakeReadOnlyDocker(), "shop-prod", set())


def test_hornet_own_twins_are_never_profiled():
    containers = shop_containers()
    twin = shop_containers()[1]
    twin["summary"]["Labels"]["com.hornet.managed"] = "true"
    twin["summary"]["Id"] = "d" * 64
    twin["inspect"]["Id"] = "d" * 64
    raw = collect(FakeReadOnlyDocker(containers + [twin]), "shop-prod", ALLOWED)
    assert len(raw.services) == 3


def test_environment_variable_values_are_dropped_at_the_source():
    raw = collect(FakeReadOnlyDocker(), "shop-prod", ALLOWED)
    web = next(s for s in raw.services if s.name == "web")
    assert "ADMIN_TOKEN" in web.env_names and "DATABASE_URL" in web.env_names
    assert all("=" not in n for n in web.env_names)
    assert CANARIES["admin_token"] not in json.dumps(web.__dict__, default=lambda o: o.__dict__)


def test_no_containers_is_an_error():
    with pytest.raises(DiscoveryError):
        collect(FakeReadOnlyDocker([]), "shop-prod", ALLOWED)


# ---- the canary test: nothing planted anywhere survives sanitization --------------------------------
def test_no_canary_survives_sanitization():
    profile = sanitize(collect(FakeReadOnlyDocker(), "shop-prod", ALLOWED))
    blob = profile.model_dump_json()
    for name, canary in CANARIES.items():
        assert canary not in blob, f"canary {name} leaked into the sanitized profile"
    assert_clean(profile, forbidden=ALL_CANARY_STRINGS)

    kinds = profile.report.findings_by_kind
    for expected in ("url_credentials", "kv_secret", "aws_access_key", "jwt", "email", "ipv4", "cli_secret"):
        assert kinds.get(expected, 0) >= 1, f"{expected} was planted but never found"
    assert profile.report.dropped_env_values >= 7
    assert profile.report.dropped_mount_sources == 1
    assert profile.report.dropped_labels >= 2  # compose bookkeeping labels, incl. the host working dir
    assert {s.role for s in profile.services} == {"redis", "postgres", "http-app"}


def test_sanitized_profile_keeps_what_the_builder_needs():
    profile = sanitize(collect(FakeReadOnlyDocker(), "shop-prod", ALLOWED))
    web = next(s for s in profile.services if s.name == "web")
    assert web.image.startswith("registry.example.internal/")  # private registry host generalised
    assert web.ports[0].container_port == 8080 and web.ports[0].bind == "127.0.0.1"
    assert web.env_classes["ADMIN_TOKEN"] == "secret" and web.env_classes["DATABASE_URL"] == "connection"
    assert web.mounts[0].destination == "/var/log/shop"
    assert "com.docker.compose.project.working_dir" not in web.labels
    assert web.labels.get("com.example.team") == "storefront"
    assert len(profile.profile_hash) == 64


def test_sanitization_is_deterministic():
    a = sanitize(collect(FakeReadOnlyDocker(), "shop-prod", ALLOWED))
    b = sanitize(collect(FakeReadOnlyDocker(), "shop-prod", ALLOWED))
    assert a.profile_hash == b.profile_hash


def test_second_pass_catches_what_the_first_missed():
    profile = sanitize(collect(FakeReadOnlyDocker(), "shop-prod", ALLOWED))
    profile.services.append(ServiceSpec(name="x", image="x", command=["--password", "hunter2pass"]))
    with pytest.raises(SanitizationFailure):
        assert_clean(profile)
    clean = sanitize(collect(FakeReadOnlyDocker(), "shop-prod", ALLOWED))
    with pytest.raises(SanitizationFailure):
        assert_clean(clean, forbidden=("storefront",))
