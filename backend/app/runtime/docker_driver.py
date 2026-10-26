"""Hardened Docker driver (dev on Windows/Linux). Weaker than gVisor/microVM: the kernel is shared.

Isolation applied to every twin:
  no network, no mounts, no Docker socket, read-only rootfs + size-limited tmpfs for the writable
  paths, all capabilities dropped, no-new-privileges, default seccomp, memory/CPU/pids/nofile limits,
  non-root users, no logging, and a label so orphans can be found and removed.
"""
import json
import threading
import time

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.types import LogConfig, Ulimit

from app.runtime.driver import (
    ExecResult,
    FsEntry,
    ImageMismatch,
    RuntimeKilled,
    RuntimeSpec,
    RuntimeUnavailable,
)
from app.runtime.fsdiff import FIND_FORMAT, parse_find
from app.runtime.policy import ExecutionPolicy

LABEL = "com.hornet.managed"
SERVICES_CFG = "/usr/share/hornet/services.json"
EMULATOR = "/usr/share/hornet/emulator.py"
EVENTS_FILE = "/run/hornet/events.jsonl"
EVENTS_DIR = "/run/hornet"
FS_SCAN_LIMIT = 1_048_576  # bytes of `find` output we will read


class HardenedDockerDriver:
    name = "hardened-docker"
    runtime: str | None = None  # Docker's default OCI runtime (runc)
    pids_overhead = 0  # threads the runtime itself needs inside the container's pids limit

    def __init__(self, client: docker.DockerClient | None = None):
        try:
            self.client = client or docker.from_env()
        except DockerException as exc:
            raise RuntimeUnavailable(str(exc)) from exc
        if self.runtime is not None and self.runtime not in self.available_runtimes():
            raise RuntimeUnavailable(f"Docker has no '{self.runtime}' runtime (see docs/user-guide.md, gVisor)")

    def available_runtimes(self) -> set[str]:
        try:
            return set((self.client.info().get("Runtimes") or {}).keys())
        except DockerException as exc:
            raise RuntimeUnavailable(str(exc)) from exc

    # -- health -----------------------------------------------------------------------------
    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except DockerException:
            return False

    def resolve_image(self, ref: str) -> str:
        try:
            return self.client.images.get(ref).id
        except ImageNotFound:
            raise ImageMismatch(f"image {ref!r} not found") from None
        except DockerException as exc:
            raise RuntimeUnavailable(str(exc)) from exc

    # -- lifecycle --------------------------------------------------------------------------
    def create(self, spec: RuntimeSpec, policy: ExecutionPolicy) -> str:
        tmpfs = {
            path: f"rw,nosuid,nodev,size={policy.tmpfs_mb}m,mode=1777"
            for path in spec.writable_paths
        }
        # the attacker's home must belong to the attacker
        uid, gid = spec.attacker_user.split(":")
        tmpfs[spec.workdir] = f"rw,nosuid,nodev,size={policy.tmpfs_mb}m,uid={uid},gid={gid},mode=0755"
        # private folder for the fake services' event log: only the system user (not the attacker) can read it
        sys_uid, sys_gid = spec.system_user.split(":")
        tmpfs[EVENTS_DIR] = f"rw,nosuid,nodev,noexec,size=4m,uid={sys_uid},gid={sys_gid},mode=0700"
        mem = f"{policy.memory_mb}m"
        try:
            container = self.client.containers.run(
                spec.image,
                command=["sleep", "infinity"],
                name=f"hornet-twin-{spec.session_id[:12]}",
                detach=True,
                hostname=spec.hostname,
                user=spec.system_user,
                working_dir=spec.workdir,
                environment=spec.env,
                network_mode="none",  # loopback only; service names resolve to loopback aliases via extra_hosts
                extra_hosts=spec.extra_hosts or None,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                privileged=False,
                read_only=True,
                tmpfs=tmpfs,
                mem_limit=mem,
                memswap_limit=mem,
                nano_cpus=int(policy.cpus * 1_000_000_000),
                pids_limit=policy.pids + self.pids_overhead,
                ulimits=[Ulimit(name="nofile", soft=policy.nofile, hard=policy.nofile)],
                init=True,
                runtime=self.runtime,
                log_config=LogConfig(type=LogConfig.types.NONE),
                labels={LABEL: "true", "com.hornet.session_id": spec.session_id},
            )
        except ImageNotFound:
            raise ImageMismatch(f"image {spec.image!r} not found") from None
        except (APIError, DockerException) as exc:
            raise RuntimeUnavailable(str(exc)) from exc
        self._seed_writable_paths(container.id, spec)
        return container.id

    def start_services(self, runtime_id: str, spec: RuntimeSpec, policy: ExecutionPolicy) -> str:
        """Start the in-twin service emulators (if this image has any) as the system user and wait until ready."""
        try:
            _, _, _, code, _ = self._run_exec(runtime_id, ["/usr/bin/test", "-f", SERVICES_CFG], spec.system_user, "/", 256, 5.0)
            if code != 0:
                return "none"
            api = self.client.api
            ex = api.exec_create(runtime_id, ["/usr/bin/python3", EMULATOR, SERVICES_CFG], user=spec.system_user, workdir="/",
                                 environment={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"})
            api.exec_start(ex["Id"], detach=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                events, _ = self.collect_service_events(runtime_id, spec, 0)
                if any(e.get("type") == "ready" for e in events):
                    return "started"
                time.sleep(0.15)
            return "failed"
        except DockerException:
            return "failed"

    def collect_service_events(self, runtime_id: str, spec: RuntimeSpec, offset: int) -> tuple[list[dict], int]:
        try:
            out, _, _, code, hung = self._run_exec(
                runtime_id, ["/usr/bin/tail", "-c", f"+{offset + 1}", EVENTS_FILE], spec.system_user, "/", 1_048_576, 5.0)
        except DockerException:
            return [], offset
        end = out.rfind(b"\n")
        if code != 0 or hung or end < 0:
            return [], offset
        events = []
        for line in out[: end + 1].splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue  # a half-written or corrupted line is skipped, never trusted
            if isinstance(ev, dict):
                events.append(ev)
        return events, offset + end + 1

    def _seed_writable_paths(self, runtime_id: str, spec: RuntimeSpec) -> None:
        """The writable paths are empty tmpfs mounts. Copy in whatever the snapshot image stored under
        /usr/share/hornet/seed (e.g. the attacker's home with its shell history and bait files)."""
        script = 'for p in "$@"; do s="/usr/share/hornet/seed$p"; [ -d "$s" ] && cp -a "$s/." "$p/" 2>/dev/null; done; exit 0'
        try:
            self._run_exec(runtime_id, ["/bin/sh", "-c", script, "sh", *spec.writable_paths],
                           spec.attacker_user, spec.workdir, 4096, 10.0)
        except DockerException:
            pass  # a twin without seed files is still a valid twin

    def destroy(self, runtime_id: str) -> None:
        try:
            self.client.containers.get(runtime_id).remove(force=True, v=True)
        except NotFound:
            return
        except DockerException as exc:
            raise RuntimeUnavailable(str(exc)) from exc

    def is_running(self, runtime_id: str) -> bool:
        try:
            c = self.client.containers.get(runtime_id)
            c.reload()
            return c.status == "running"
        except DockerException:
            return False

    def list_managed(self) -> list[str]:
        try:
            return [c.id for c in self.client.containers.list(all=True, filters={"label": f"{LABEL}=true"})]
        except DockerException as exc:
            raise RuntimeUnavailable(str(exc)) from exc

    def processes(self, runtime_id: str) -> list[dict]:
        try:
            top = self.client.containers.get(runtime_id).top()
        except DockerException:
            return []
        titles = top.get("Titles", [])
        return [dict(zip(titles, row, strict=False)) for row in (top.get("Processes") or [])[:100]]

    # -- execution --------------------------------------------------------------------------
    def _run_exec(
        self, runtime_id: str, argv: list[str], user: str, workdir: str, limit: int, deadline_s: float,
        on_truncate=None,
    ) -> tuple[bytes, bytes, bool, int | None, bool]:
        """Run argv inside the runtime. Returns (stdout, stderr, truncated, exit_code, hung).

        `hung` means our wall-clock deadline passed before the exec finished. When output passes
        `limit` we stop storing it, call `on_truncate` (which kills the runaway process) and keep
        draining the stream until it closes, so no half-read connection is left behind."""
        api = self.client.api
        ex = api.exec_create(runtime_id, argv, stdout=True, stderr=True, user=user, workdir=workdir)
        out, err = bytearray(), bytearray()
        state = {"truncated": False}

        def reader() -> None:
            try:
                for o, e in api.exec_start(ex["Id"], stream=True, demux=True):
                    for chunk, buf in ((o, out), (e, err)):
                        if not chunk:
                            continue
                        room = limit - len(out) - len(err)
                        if room <= 0:
                            state["truncated"] = True  # more data than we are willing to keep
                            continue
                        buf.extend(chunk[:room])
                        if len(chunk) > room:
                            state["truncated"] = True
            except Exception:  # noqa: BLE001  (connection torn down on kill)
                pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        end = time.monotonic() + deadline_s
        killed = False
        while t.is_alive() and time.monotonic() < end:
            t.join(0.05)
            if state["truncated"] and not killed and on_truncate is not None:
                killed = True
                on_truncate()
        hung = t.is_alive()
        exit_code = None
        if not hung:
            try:
                exit_code = api.exec_inspect(ex["Id"]).get("ExitCode")
            except DockerException:
                exit_code = None
        return bytes(out), bytes(err), state["truncated"], exit_code, hung

    def _kill_attacker_processes(self, runtime_id: str, spec: RuntimeSpec) -> None:
        """Best effort: signal every process owned by the attacker uid. PID 1 runs as another uid."""
        try:
            self._run_exec(runtime_id, ["/bin/kill", "-KILL", "-1"], spec.attacker_user, spec.workdir, 1024, 2.0)
        except DockerException:
            pass

    def exec(self, runtime_id: str, spec: RuntimeSpec, command: str, policy: ExecutionPolicy) -> ExecResult:
        secs = policy.timeout_ms / 1000
        argv = ["/usr/bin/timeout", "-s", "KILL", f"{secs:.3f}", "/bin/bash", "-c", command]
        started = time.monotonic()
        try:
            out, err, truncated, code, hung = self._run_exec(
                runtime_id, argv, spec.attacker_user, spec.workdir,
                policy.output_limit_bytes, secs + policy.kill_grace_ms / 1000,
                on_truncate=lambda: self._kill_attacker_processes(runtime_id, spec),
            )
        except (APIError, DockerException) as exc:
            raise RuntimeUnavailable(str(exc)) from exc
        duration_ms = int((time.monotonic() - started) * 1000)

        if hung:
            # The in-container timeout did not fire (e.g. fork bomb exhausted the pids limit).
            # Terminate the whole twin: the orchestrator treats this as a limit violation.
            try:
                self.client.containers.get(runtime_id).kill()
            except DockerException:
                pass
            return ExecResult(None, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
                              True, truncated, duration_ms, runtime_killed=True)

        timed_out = code == 137 and duration_ms >= policy.timeout_ms * 0.9
        if timed_out:
            self._kill_attacker_processes(runtime_id, spec)
        return ExecResult(code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
                          timed_out, truncated, duration_ms)

    def snapshot_fs(self, runtime_id: str, spec: RuntimeSpec, policy: ExecutionPolicy) -> list[FsEntry]:
        argv = ["/usr/bin/find", *spec.writable_paths, "-printf", FIND_FORMAT]
        try:
            out, _, _, _, hung = self._run_exec(
                runtime_id, argv, spec.attacker_user, spec.workdir, FS_SCAN_LIMIT, 10.0
            )
        except DockerException as exc:
            raise RuntimeUnavailable(str(exc)) from exc
        if hung:
            raise RuntimeKilled("filesystem observer did not finish")
        return parse_find(out)


class GVisorDriver(HardenedDockerDriver):
    """The same hardened container, run under gVisor (Docker runtime `runsc`, Linux only). The visitor's system calls
    go to gVisor's user-space kernel instead of the host kernel, which removes the shared-kernel escape path that
    plain containers have. Every other control (no network, no capabilities, limits, read-only root) is unchanged."""

    name = "gvisor"
    runtime = "runsc"
    # Under gVisor the pids cgroup counts the sandbox's own host threads (about 32 when idle), not the visitor's
    # processes one for one. Without headroom a small policy limit stops the sandbox from starting. When the limit
    # is hit, gVisor ends the whole twin (the host is unaffected), so a fork bomb still ends only that visitor.
    pids_overhead = 64
