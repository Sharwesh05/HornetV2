"""Read-only discovery of an authorized Docker environment.

* ReadOnlyDocker exposes only four read calls; it holds no reference to the raw client, so nothing
  that could start, stop, exec into, copy from or remove a production container is reachable.
* Only compose projects listed in HORNET_DISCOVERY_ALLOWED_PROJECTS can be observed.
* Environment variable values are dropped immediately: only names are kept.
"""
import re

from docker.errors import DockerException

from app.profile import RawEnvironment, RawMount, RawPort, RawService

MANAGED_LABEL = "com.hornet.managed"  # Hornet's own twins are never part of a profile
_PROJECT_LABEL = "com.docker.compose.project"
_SERVICE_LABEL = "com.docker.compose.service"
_PROJECT_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
MAX_SERVICES = 50


class DiscoveryError(Exception):
    pass


class DiscoveryNotAllowed(DiscoveryError):
    """The project is not on the operator's allowlist."""


class DiscoveryUnavailable(DiscoveryError):
    """Docker could not be reached."""


class ReadOnlyDocker:
    """The only Docker handle discovery gets."""

    def __init__(self, client) -> None:
        api = client.api
        self.__api = api  # name-mangled: not reachable as an attribute from outside

    def list_containers(self, project: str) -> list[dict]:
        return self.__api.containers(all=True, filters={"label": [f"{_PROJECT_LABEL}={project}"]})

    def inspect_container(self, container_id: str) -> dict:
        return self.__api.inspect_container(container_id)

    def container_diff(self, container_id: str) -> list[dict]:
        return self.__api.diff(container_id) or []

    def list_network_names(self, project: str) -> list[str]:
        nets = self.__api.networks(filters={"label": [f"{_PROJECT_LABEL}={project}"]})
        return sorted(n.get("Name", "") for n in nets if n.get("Name"))


def _ports(info: dict) -> list[RawPort]:
    cfg = info.get("Config") or {}
    bindings = (info.get("HostConfig") or {}).get("PortBindings") or {}
    seen: dict[tuple[int, str], RawPort] = {}
    for key in list(cfg.get("ExposedPorts") or {}) + list(bindings):
        port, _, proto = key.partition("/")
        if not port.isdigit():
            continue
        seen.setdefault((int(port), proto or "tcp"), RawPort(int(port), proto or "tcp"))
    for key, binds in bindings.items():
        port, _, proto = key.partition("/")
        for b in binds or []:
            rp = seen.get((int(port), proto or "tcp"))
            if rp and b.get("HostPort", "").isdigit():
                rp.host_ip, rp.host_port = b.get("HostIp") or None, int(b["HostPort"])
    return sorted(seen.values(), key=lambda p: (p.container_port, p.protocol))


def _service(docker: ReadOnlyDocker, summary: dict) -> RawService:
    cid = summary["Id"]
    info = docker.inspect_container(cid)
    cfg = info.get("Config") or {}
    labels = cfg.get("Labels") or {}
    name = labels.get(_SERVICE_LABEL) or (info.get("Name") or cid[:12]).strip("/")

    hc = (cfg.get("Healthcheck") or {}).get("Test") or []
    try:
        diff_paths = [d.get("Path", "") for d in docker.container_diff(cid)][:200]
    except DockerException:
        diff_paths = []

    return RawService(
        name=name,
        container_name=(info.get("Name") or "").strip("/"),
        image=cfg.get("Image") or "unknown",
        # NAMES ONLY. The values in "NAME=value" are discarded right here.
        env_names=[e.split("=", 1)[0] for e in (cfg.get("Env") or []) if isinstance(e, str)],
        entrypoint=list(cfg.get("Entrypoint") or []),
        command=list(cfg.get("Cmd") or []),
        labels=dict(labels),
        ports=_ports(info),
        networks=sorted(((info.get("NetworkSettings") or {}).get("Networks") or {}).keys()),
        mounts=[
            RawMount(kind=m.get("Type", "volume"), destination=m.get("Destination", ""), read_only=not m.get("RW", True))
            for m in (info.get("Mounts") or [])
        ],
        user=cfg.get("User") or None,
        workdir=cfg.get("WorkingDir") or None,
        healthcheck=" ".join(hc[1:] if hc and hc[0] in ("CMD", "CMD-SHELL") else hc) or None,
        fs_paths=diff_paths,
    )


def collect(docker: ReadOnlyDocker, project: str, allowed_projects: set[str]) -> RawEnvironment:
    if not _PROJECT_OK.match(project or ""):
        raise DiscoveryError("invalid project name")
    if project not in allowed_projects:
        raise DiscoveryNotAllowed(f"project {project!r} is not in the discovery allowlist")
    try:
        summaries = [c for c in docker.list_containers(project) if MANAGED_LABEL not in (c.get("Labels") or {})]
        if not summaries:
            raise DiscoveryError(f"no containers found for project {project!r}")
        if len(summaries) > MAX_SERVICES:
            raise DiscoveryError(f"project has more than {MAX_SERVICES} containers")
        services = [_service(docker, c) for c in summaries]
        networks = docker.list_network_names(project)
    except DockerException as exc:
        raise DiscoveryUnavailable(str(exc)) from exc
    return RawEnvironment(project=project, services=services, networks=networks)
