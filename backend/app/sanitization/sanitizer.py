"""Turn what discovery saw into a profile that is safe to store and to show to an AI provider.

Policy (enforced here, tested with planted canary secrets):
  * environment variable VALUES never get this far (discovery keeps names only);
  * every free-text field (command, entrypoint, labels, healthcheck, paths) is scanned and redacted;
  * host-side paths, private IPs, emails and compose bookkeeping labels are removed;
  * a second pass re-scans the result, and any leftover finding fails the build (assert_clean).
"""
import hashlib
import json
import re
from collections import Counter

from app.environment.role_detect import detect_role
from app.profile import (
    MountSpec,
    PortSpec,
    RawEnvironment,
    RawService,
    SanitizationReport,
    SanitizedProfile,
    ServiceSpec,
)
from app.sanitization.classifier import classify_env_name
from app.sanitization.patterns import SECRET_FLAG, find_secrets, redact

_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_PUBLIC_REGISTRIES = ("docker.io", "ghcr.io", "quay.io", "gcr.io", "mcr.microsoft.com", "registry.k8s.io")
_KEEP_COMPOSE_LABELS = {"com.docker.compose.service", "com.docker.compose.project"}
_MAX_FS_HINTS = 100


class SanitizationFailure(Exception):
    """Something sensitive survived sanitization; the build must be rejected."""


class Sanitizer:
    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._report = SanitizationReport()

    # -- helpers ------------------------------------------------------------------------------
    def _clean(self, text: str | None) -> str | None:
        if text is None:
            return None
        cleaned, findings = redact(text)
        self._counts.update(f.kind for f in findings)
        return cleaned

    def _argv(self, args: list[str]) -> list[str]:
        """Clean a command line. A value that follows a flag like --token is redacted whole, and the
        remaining elements are scanned both alone and as one joined command line."""
        out, prev_secret_flag = [], False
        for a in args:
            if prev_secret_flag:
                self._counts["cli_secret"] += 1
                out.append("<REDACTED:cli_secret>")
            else:
                out.append(self._clean(a) or "")
            prev_secret_flag = bool(SECRET_FLAG.match(a))
        joined, findings = redact(" ".join(out))
        if findings:  # something only visible across elements (e.g. 'curl -u user:pass')
            self._counts.update(f.kind for f in findings)
            return joined.split(" ")
        return out

    def _name(self, raw: str, fallback: str) -> str:
        n = raw.strip("/").lower()
        n = re.sub(r"[^a-z0-9_.-]", "-", n)
        return n if _NAME_OK.match(n) else fallback

    def _image(self, ref: str) -> str:
        ref = self._clean(ref) or "unknown"
        first = ref.split("/", 1)[0]
        if "/" in ref and ("." in first or ":" in first) and first not in _PUBLIC_REGISTRIES:
            self._counts["private_registry"] += 1
            ref = "registry.example.internal/" + ref.split("/", 1)[1]
        return ref

    # -- one service --------------------------------------------------------------------------
    def _service(self, raw: RawService, index: int) -> ServiceSpec:
        labels: dict[str, str] = {}
        for k, v in raw.labels.items():
            if k.startswith("com.docker.compose.") and k not in _KEEP_COMPOSE_LABELS:
                self._report.dropped_labels += 1
                continue
            labels[self._clean(k) or ""] = self._clean(v) or ""

        env_names = sorted({n for n in raw.env_names if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", n)})
        self._report.dropped_env_values += len(env_names)  # values were never collected

        ports = []
        for p in raw.ports:
            bind = p.host_ip
            if bind and bind not in ("127.0.0.1", "0.0.0.0", "::", "::1"):  # noqa: S104
                self._counts["ipv4"] += 1
                bind = "0.0.0.0"  # noqa: S104  (descriptive only)
            ports.append(PortSpec(container_port=p.container_port, protocol=p.protocol, host_port=p.host_port, bind=bind))

        mounts = []
        for m in raw.mounts:
            self._report.dropped_mount_sources += 1
            mounts.append(MountSpec(destination=self._clean(m.destination) or "/", kind=m.kind, read_only=m.read_only))

        hints = []
        for path in raw.fs_paths[:_MAX_FS_HINTS]:
            cleaned = self._clean(path) or ""
            if "<REDACTED" in cleaned:
                self._report.dropped_paths += 1
                continue
            hints.append(cleaned)

        name = self._name(raw.name, f"svc-{index + 1}")
        image = self._image(raw.image)
        return ServiceSpec(
            name=name,
            role=detect_role(
                image,
                [p.container_port for p in raw.ports],
                " ".join([*(a for a in raw.entrypoint), *(a for a in raw.command)]),
                any(p.host_port for p in raw.ports),
            ),
            image=image,
            ports=ports,
            env_names=env_names,
            env_classes={n: classify_env_name(n) for n in env_names},
            networks=sorted(self._name(n, "net") for n in raw.networks),
            mounts=mounts,
            labels=labels,
            entrypoint=self._argv(raw.entrypoint),
            command=self._argv(raw.command),
            healthcheck=self._clean(raw.healthcheck),
            user=self._clean(raw.user),
            workdir=self._clean(raw.workdir),
            fs_hints=hints,
        )

    # -- whole environment ----------------------------------------------------------------------
    def sanitize(self, raw: RawEnvironment) -> SanitizedProfile:
        services = [self._service(s, i) for i, s in enumerate(sorted(raw.services, key=lambda s: s.name))]
        self._report.findings_by_kind = dict(sorted(self._counts.items()))
        profile = SanitizedProfile(
            source=self._name(raw.project, "environment"),
            services=services,
            networks=sorted(self._name(n, "net") for n in raw.networks),
            report=self._report,
        )
        profile.profile_hash = profile_hash(profile)
        assert_clean(profile)
        return profile


def profile_hash(profile: SanitizedProfile) -> str:
    body = profile.model_dump(exclude={"profile_hash", "report"})
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


_SKIP_KEYS = {"env_names", "env_classes", "profile_hash", "report"}  # names/hashes/counts, no free text


def _strings(obj, skip: bool = True):
    """Yield every free-text string in a dumped profile (dict keys of labels included)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if skip and k in _SKIP_KEYS:
                continue
            yield str(k)
            yield from _strings(v, skip)
    elif isinstance(obj, list | tuple):
        if obj and all(isinstance(v, str) for v in obj):
            yield " ".join(obj)  # secrets can span argv elements
        for v in obj:
            yield from _strings(v, skip)


def assert_clean(profile: SanitizedProfile, forbidden: tuple[str, ...] = ()) -> None:
    """Second pass over the final profile. `forbidden` lets tests (or an operator who knows a
    secret) add exact strings that must not appear anywhere."""
    kinds: set[str] = set()
    for text in _strings(profile.model_dump()):
        kinds.update(f.kind for f in find_secrets(text) if f.kind != "host_path")
        for needle in forbidden:
            if needle and needle in text:
                raise SanitizationFailure("a forbidden value survived sanitization")
    if kinds:
        raise SanitizationFailure(f"sensitive data survived sanitization: {', '.join(sorted(kinds))}")


def sanitize(raw: RawEnvironment) -> SanitizedProfile:
    return Sanitizer().sanitize(raw)
