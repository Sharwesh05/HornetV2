import json

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.ai.nim_provider import AIProviderError, NimProvider, parse_json_object
from app.config import Settings
from app.discovery.docker_inventory import collect
from app.environment import pipeline
from app.models import AdminAuditEvent, EnvironmentSnapshot
from app.sanitization import ai_review
from app.sanitization.ai_review import review_profile
from app.sanitization.sanitizer import SanitizationFailure, sanitize
from tests.helpers_discovery import (
    AI_ONLY,
    ALL_CANARY_STRINGS,
    CANARIES,
    FakeImageBuilder,
    FakeReadOnlyDocker,
)

KEY = "nvapi-" + "k" * 30
ALLOWED = {"shop-prod"}


# ---- NIM client (mock transport, no network) ---------------------------------------------------
def _provider(handler, **kw):
    return NimProvider(KEY, transport=httpx.MockTransport(handler), sleep=lambda _s: None, **kw)


def _ok(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_request_shape_and_secret_handling():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers["authorization"], json.loads(request.content)
        return _ok('{"ok": true}')

    p = _provider(handler, model="nvidia/nemotron-3-super-120b-a12b")
    out = p.generate_structured(system_policy="POLICY", context={"x": 1}, output_schema={"type": "object"})
    assert out == {"ok": True}
    assert seen["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    body = seen["body"]
    assert body["model"] == "nvidia/nemotron-3-super-120b-a12b" and body["temperature"] == 0.0 and body["stream"] is False
    assert body["nvext"] == {"guided_json": {"type": "object"}}
    assert body["messages"][0]["role"] == "system" and "POLICY" in body["messages"][0]["content"]
    assert KEY not in repr(p) and KEY not in json.dumps(body)  # the key only travels in the header


@pytest.mark.parametrize(
    "content",
    ['{"a": 1}', '```json\n{"a": 1}\n```', 'Sure! Here you go:\n{"a": 1}\nHope that helps.'],
)
def test_json_extraction(content):
    assert parse_json_object(content) == {"a": 1}


@pytest.mark.parametrize("content", ["no json here", "[1, 2]", '{"a": ', ""])
def test_bad_model_output_is_an_error(content):
    with pytest.raises(AIProviderError):
        parse_json_object(content)


def test_auth_failure_never_leaks_the_key():
    p = _provider(lambda r: httpx.Response(401, json={"detail": f"bad key {KEY}"}))
    with pytest.raises(AIProviderError) as exc:
        p.generate_structured(system_policy="s", context={}, output_schema={})
    assert KEY not in str(exc.value) and "API key" in str(exc.value)


def test_retries_transient_errors_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else _ok('{"done": 1}')

    assert _provider(handler).generate_structured(system_policy="s", context={}, output_schema={}) == {"done": 1}
    assert len(calls) == 3


def test_gives_up_after_bounded_retries():
    calls = []
    p = _provider(lambda r: calls.append(1) or httpx.Response(429), max_retries=2)
    with pytest.raises(AIProviderError):
        p.generate_structured(system_policy="s", context={}, output_schema={})
    assert len(calls) == 3


def test_timeout_is_an_error():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(AIProviderError) as exc:
        _provider(handler, max_retries=0).generate_structured(system_policy="s", context={}, output_schema={})
    assert "timed out" in str(exc.value)


def test_reasoning_text_is_ignored_and_token_budget_is_configurable():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return _ok('<think>let me consider { braces } first</think>\n{"a": 1}')

    out = _provider(handler, max_tokens=8192).generate_structured(system_policy="s", context={}, output_schema={})
    assert out == {"a": 1} and seen["body"]["max_tokens"] == 8192


def test_truncated_model_output_is_reported_clearly():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": '{"findings": [{"fie'}}]})

    with pytest.raises(AIProviderError) as exc:
        _provider(handler).generate_structured(system_policy="s", context={}, output_schema={})
    assert "cut off" in str(exc.value) and "MAX_TOKENS" in str(exc.value)


def test_falls_back_without_nvext_when_the_server_rejects_it():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(400) if "nvext" in body else _ok('{"v": 2}')

    assert _provider(handler).generate_structured(system_policy="s", context={}, output_schema={}) == {"v": 2}
    assert "nvext" in bodies[0] and "nvext" not in bodies[1]


@pytest.mark.parametrize("resp", [
    httpx.Response(200, json={"choices": []}), httpx.Response(200, json={"nope": 1}),
    httpx.Response(200, json={"choices": [{"message": {"content": None}}]}), httpx.Response(200, text="<html>"),
    httpx.Response(500),
])
def test_unexpected_responses_are_errors(resp):
    with pytest.raises(AIProviderError):
        _provider(lambda r: resp, max_retries=0).generate_structured(system_policy="s", context={}, output_schema={})


def test_settings_for_nim(monkeypatch):
    base = dict(admin_api_key="x" * 20, _env_file=None)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(ai_provider="nim", **base)  # no key
    with pytest.raises(ValidationError):
        Settings(ai_provider="nim", nvidia_api_key=KEY, nim_base_url="http://evil.example/v1", **base)
    s = Settings(ai_provider="nim", nvidia_api_key=KEY, ai_sanitizer_mode="required", **base)
    assert s.sanitizer_mode == "required" and KEY not in repr(s) and KEY not in s.model_dump_json()
    assert Settings(ai_provider="nim", nvidia_api_key=KEY, nim_base_url="http://localhost:8000/v1", **base)
    assert Settings(**base).sanitizer_mode == "off"  # template provider: no model review
    monkeypatch.setenv("NVIDIA_API_KEY", KEY)
    assert Settings(ai_provider="nim", **base).nvidia_api_key.get_secret_value() == KEY


# ---- AI sanitization review ---------------------------------------------------------------------
class Scripted:
    name, version, is_model = "scripted", "1", True

    def __init__(self, responder):
        self.responder, self.contexts = responder, []

    def generate_structured(self, *, system_policy, context, output_schema):
        self.contexts.append(context)
        return self.responder(context)


def semantic(context):
    """Behaves like a competent reviewer: flags the things only a model could know about."""
    findings = []
    for f in context["fields"]:
        for cat, needle in (("personal_data", AI_ONLY["person"]), ("internal_host", AI_ONLY["internal_url"]),
                            ("identifier", AI_ONLY["account"])):
            if needle in f["text"]:
                findings.append({"field_id": f["id"], "substring": needle, "category": cat, "confidence": 0.95})
    return {"findings": findings, "secret_env_names": [n for n in context["env_names"] if n == "ACME_LICENSE"]}


@pytest.fixture
def base():
    return sanitize(collect(FakeReadOnlyDocker(), "shop-prod", ALLOWED))


def _web(profile):
    return next(s for s in profile.services if s.name == "web")


def test_model_sees_only_already_redacted_text(base):
    reviewer = Scripted(semantic)
    review_profile(base, reviewer, "required")
    sent = json.dumps(reviewer.contexts)
    for name, canary in CANARIES.items():
        assert canary not in sent, f"canary {name} was sent to the model"
    for secret_value in ("lic-canary-value",):  # an env VALUE: never even collected
        assert secret_value not in sent
    assert "env_names" in sent and "ACME_LICENSE" in sent  # names only
    assert AI_ONLY["person"] in sent  # the model does see what regexes cannot classify: that is its job


def test_model_findings_are_applied_and_counted(base):
    out = review_profile(base, Scripted(semantic), "required")
    blob = out.model_dump_json()
    for needle in AI_ONLY.values():
        assert needle not in blob
    web = _web(out)
    assert web.labels["owner.contact"] == "redacted-personal_data (payroll team)"
    assert web.labels["internal.docs"] == "https://redacted-internal_host/runbook"
    assert web.env_classes["ACME_LICENSE"] == "secret"  # raised from config
    r = out.report.ai_review
    assert r["findings_applied"] == 3 and r["env_classes_raised"] == 1 and r["errors"] == 0
    assert r["by_category"] == {"personal_data": 1, "internal_host": 1, "identifier": 1}
    assert json.dumps(r).count("Dana") == 0  # counts only, never the removed text
    assert out.profile_hash != base.profile_hash
    assert base.model_dump_json().count(AI_ONLY["person"]) == 1  # the input profile is untouched


def test_model_cannot_add_or_rewrite_anything(base):
    def creative(context):
        f = context["fields"][0]
        return {"findings": [{"field_id": f["id"], "substring": "x" * 10, "category": "other"}], "secret_env_names": []}

    out = review_profile(base, Scripted(creative), "required")
    assert out.model_dump(exclude={"report", "profile_hash"}) == base.model_dump(exclude={"report", "profile_hash"})
    assert out.report.ai_review["findings_rejected"] >= 1


def test_every_changed_word_comes_from_the_original_or_is_a_placeholder(base):
    out = review_profile(base, Scripted(semantic), "required")
    for old, new in zip(_web(base).labels.values(), _web(out).labels.values(), strict=True):
        extra = set(new.replace("(", " ").replace(")", " ").split()) - set(old.replace("(", " ").replace(")", " ").split())
        assert all(w.startswith(("redacted-", "https://redacted-")) for w in extra), extra


@pytest.mark.parametrize(
    ("name", "finding"),
    [
        ("unknown field", {"field_id": "zzz", "substring": "Dana Whitfield", "category": "personal_data"}),
        ("not verbatim", {"field_id": "lv0", "substring": "Not In The Text", "category": "other"}),
        ("too short", {"field_id": "lv0", "substring": "ab", "category": "other"}),
        ("targets a marker", {"field_id": "lv0", "substring": "<REDACTED:kv", "category": "other"}),
        ("low confidence", {"field_id": "lv0", "substring": "payroll", "category": "other", "confidence": 0.1}),
    ],
)
def test_invalid_findings_are_rejected(base, name, finding):
    out = review_profile(base, Scripted(lambda c: {"findings": [finding], "secret_env_names": []}), "required")
    assert out.report.ai_review["findings_applied"] == 0 and out.report.ai_review["findings_rejected"] >= 1, name


def test_over_redaction_of_structural_fields_is_refused(base):
    def erase_workdir(context):
        f = next((f for f in context["fields"] if f["kind"] == "workdir"), None)
        if f is None:
            return {"findings": [], "secret_env_names": []}
        return {"findings": [{"field_id": f["id"], "substring": f["text"], "category": "other"}], "secret_env_names": []}

    out = review_profile(base, Scripted(erase_workdir), "required")
    assert _web(out).workdir == _web(base).workdir == "/var/www/shop"  # the builder still gets a usable path
    assert out.report.ai_review["findings_applied"] == 0 and out.report.ai_review["findings_rejected"] == 1


def test_a_label_that_is_entirely_sensitive_can_be_erased(base):
    def erase_owner(context):
        f = next((f for f in context["fields"] if f["text"].startswith(AI_ONLY["person"])), None)
        findings = [{"field_id": f["id"], "substring": f["text"], "category": "personal_data"}] if f else []
        return {"findings": findings, "secret_env_names": []}

    out = review_profile(base, Scripted(erase_owner), "required")
    assert _web(out).labels["owner.contact"] == "redacted-personal_data"


def test_model_can_only_mark_env_vars_as_secret(base):
    def answer(context):
        return {"findings": [], "secret_env_names": ["PORT", "ADMIN_TOKEN", "INVENTED"]}

    out = review_profile(base, Scripted(answer), "required")
    web = _web(out)
    assert web.env_classes["PORT"] == "secret"          # tightened
    assert web.env_classes["ADMIN_TOKEN"] == "secret"   # already secret: unchanged
    assert "INVENTED" not in web.env_classes            # cannot add names
    assert out.report.ai_review["env_classes_raised"] == 1  # only PORT changed


def test_connection_variables_keep_their_class_so_urls_stay_consistent(base):
    """Regression (found live): the model flagged DATABASE_URL/REDIS_URL as secret-bearing; downgrading them from
    'connection' made the builder emit random tokens instead of URLs that match the database credentials."""
    def flag_urls(context):
        return {"findings": [], "secret_env_names": [n for n in context["env_names"] if n.endswith("_URL")]}

    out = review_profile(base, Scripted(flag_urls), "required")
    assert _web(out).env_classes["DATABASE_URL"] == "connection" and _web(out).env_classes["REDIS_URL"] == "connection"
    assert out.report.ai_review["env_classes_raised"] == 0


def test_structural_compose_labels_are_never_sent_or_altered(base):
    reviewer = Scripted(semantic)
    out = review_profile(base, reviewer, "required")
    sent = json.dumps(reviewer.contexts)
    assert "com.docker.compose" not in sent and "shop-prod" not in json.dumps([f for c in reviewer.contexts for f in c["fields"]])
    assert _web(out).labels["com.docker.compose.project"] == "shop-prod"


@pytest.mark.parametrize("kept", ["shop-prod", "shop", "web", "SHOP"])
def test_findings_against_kept_names_are_rejected(base, kept):
    """Regression (found live): the model flagged the project/product name, which the twin intentionally keeps."""
    def flag_project(context):
        f = next((f for f in context["fields"] if f["kind"] == "healthcheck"), None)
        assert "shop-prod" in context["keep_terms"] and "web" in context["keep_terms"]
        fid = f["id"] if f else context["fields"][0]["id"]
        text = f["text"] if f else context["fields"][0]["text"]
        return {"findings": [{"field_id": fid, "substring": kept, "category": "identifier"}] if kept in text else [],
                "secret_env_names": []}

    out = review_profile(base, Scripted(flag_project), "required")
    assert out.report.ai_review["findings_applied"] == 0
    assert out.model_dump(exclude={"report", "profile_hash"}) == base.model_dump(exclude={"report", "profile_hash"})


def test_calls_run_in_parallel_but_results_are_applied_deterministically(base):
    import threading
    import time

    seen, lock = set(), threading.Lock()

    def slow(context):
        with lock:
            seen.add(threading.get_ident())
        time.sleep(0.05)
        return semantic(context)

    a = review_profile(base, Scripted(slow), "required")
    b = review_profile(base, Scripted(semantic), "required")
    assert len(seen) > 1                       # more than one worker thread was used
    assert a.model_dump() == b.model_dump()    # and the outcome does not depend on timing


@pytest.mark.parametrize("bad", [{"findings": "nope"}, "text", {"findings": [{"field_id": 1}], "secret_env_names": []}])
def test_schema_violations_fail_a_required_review(base, bad):
    with pytest.raises(SanitizationFailure):
        review_profile(base, Scripted(lambda c: bad), "required")


def test_provider_outage_fails_closed_when_required_and_degrades_when_advisory(base):
    def down(_context):
        raise AIProviderError("NIM HTTP 503")

    with pytest.raises(SanitizationFailure):
        review_profile(base, Scripted(down), "required")
    out = review_profile(base, Scripted(down), "advisory")
    assert out.report.ai_review["errors"] == 3 and out.report.ai_review["findings_applied"] == 0
    assert out.model_dump(exclude={"report", "profile_hash"}) == base.model_dump(exclude={"report", "profile_hash"})


def test_off_mode_does_not_call_the_model(base):
    reviewer = Scripted(semantic)
    assert review_profile(base, reviewer, "off") is base and reviewer.contexts == []


def test_placeholders_are_filesystem_safe():
    assert all(c not in ai_review._safe_placeholder("org_name") for c in '<>:"|?*\\')


# ---- pipeline ---------------------------------------------------------------------------------------
def _build(db, tmp_path, provider, mode):
    return pipeline.build_from_docker(
        db, docker=FakeReadOnlyDocker(), image_builder=FakeImageBuilder(), provider=provider, project="shop-prod",
        allowed_projects=ALLOWED, base_image="b", build_dir=tmp_path, actor="t", sanitizer_mode=mode,
        forbidden=ALL_CANARY_STRINGS + tuple(AI_ONLY.values()),
    )


def test_pipeline_applies_ai_review_and_records_it(db, tmp_path):
    snap = _build(db, tmp_path, Scripted(semantic), "required")
    build = snap.validation_report["build"]
    assert build["sanitization"]["ai_review"]["findings_applied"] == 3
    assert AI_ONLY["person"] not in json.dumps(build)
    # the model-flagged "license" variable became a secret, so the twin got a fake credential for it
    assert any(k.endswith("ACME_LICENSE") for k in _plan_secret_names(build))


def _plan_secret_names(build):
    web = next(s for s in build["profile"]["services"] if s["name"] == "web")
    return [f"web.{n}" for n, c in web["env_classes"].items() if c == "secret"]


def test_required_review_failure_rejects_the_build_before_anything_is_built(db, tmp_path):
    def down(_c):
        raise AIProviderError("NIM HTTP 503")

    builder = FakeImageBuilder()
    with pytest.raises(pipeline.BuildFailed) as exc:
        pipeline.build_from_docker(
            db, docker=FakeReadOnlyDocker(), image_builder=builder, provider=Scripted(down), project="shop-prod",
            allowed_projects=ALLOWED, base_image="b", build_dir=tmp_path, actor="t", sanitizer_mode="required",
        )
    assert exc.value.stage == "sanitization" and builder.built == []
    assert db.scalars(select(EnvironmentSnapshot)).all() == []
    assert "security.sanitization_failed" in {a.action for a in db.scalars(select(AdminAuditEvent))}


def test_advisory_mode_builds_with_the_deterministic_result(db, tmp_path):
    def down(_c):
        raise AIProviderError("NIM HTTP 503")

    snap = pipeline.build_from_docker(
        db, docker=FakeReadOnlyDocker(), image_builder=FakeImageBuilder(), provider=Scripted(down), project="shop-prod",
        allowed_projects=ALLOWED, base_image="b", build_dir=tmp_path, actor="t", sanitizer_mode="advisory",
    )
    assert snap.status == "validated" and snap.validation_report["build"]["sanitization"]["ai_review"]["errors"] == 3


def test_template_provider_never_triggers_a_review(db, tmp_path):
    from app.ai.provider import TemplateProvider

    snap = _build(db, tmp_path, TemplateProvider(), "required") if False else pipeline.build_from_docker(
        db, docker=FakeReadOnlyDocker(), image_builder=FakeImageBuilder(), provider=TemplateProvider(),
        project="shop-prod", allowed_projects=ALLOWED, base_image="b", build_dir=tmp_path, actor="t",
        sanitizer_mode="required",
    )
    assert snap.validation_report["build"]["sanitization"]["ai_review"] == {}
