"""In-memory RuntimeDriver for unit tests (no Docker needed)."""
import itertools

from app.runtime.driver import ExecResult, FsEntry, ImageMismatch, RuntimeSpec


class FakeDriver:
    name = "fake"

    def __init__(self, images: dict[str, str] | None = None):
        self.runtimes: dict[str, dict] = {}
        self.images = images or {}
        self._ids = itertools.count(1)
        self.destroyed: list[str] = []
        self.up = True

    def ping(self):
        return self.up

    def resolve_image(self, ref):
        if ref in self.images:
            return self.images[ref]
        if ref.startswith("sha256:") or ref.startswith("hornet-twin-base:"):
            return ref
        raise ImageMismatch(ref)

    def create(self, spec: RuntimeSpec, policy):
        rid = f"rt-{next(self._ids)}"
        self.runtimes[rid] = {"spec": spec, "files": {}, "alive": True}
        return rid

    def exec(self, runtime_id, spec, command, policy):
        rt = self.runtimes[runtime_id]
        if "HORNET:" in command:
            for line in command.split("\n")[1:]:  # special test commands behave the same inside the stateful wrapper
                if line in ("saturate", "cannot-fork", "flood") or line.startswith("hit "):
                    return self.exec(runtime_id, spec, line, policy)
            return self._stateful(rt, command, policy)
        if command.startswith("touch "):
            rt["files"][command[6:]] = 0
            return ExecResult(0, "", "", False, False, 3)
        if command.startswith("echo "):
            rt["files"]["/tmp/out"] = len(command)
            return ExecResult(0, command[5:] + "\n", "", False, False, 2)
        if command == "sleep-forever":
            return ExecResult(137, "", "", True, False, policy.timeout_ms)
        if command == "bomb":
            rt["alive"] = False
            return ExecResult(None, "", "", True, False, policy.timeout_ms + 3000, runtime_killed=True)
        if command == "saturate":  # a fork bomb that returns normally but leaves the process table full
            rt["procs"] = 10_000
            return ExecResult(0, "", "", False, False, 5)
        if command == "cannot-fork":
            return ExecResult(125, "", "/usr/bin/timeout: fork system call failed: Resource temporarily unavailable\n",
                              False, False, 5)
        if command.startswith("hit "):  # simulate an attacker request to a fake service
            rt.setdefault("events", []).append({"ts": "2026-10-13T04:30:00.000+00:00", "service": command[4:], "proto": "http",
                                                "action": "GET /api/customers", "status": 200, "remote": "127.0.0.1"})
            return ExecResult(0, "{}\n", "", False, False, 4)
        if command == "flood":
            return ExecResult(0, "x" * policy.output_limit_bytes, "", False, True, 5)
        return ExecResult(127, "", f"{command}: command not found\n", False, False, 1)

    def _stateful(self, rt, script, policy):
        """Interpret the ShellState wrapper just enough for tests: cd, export, echo, pwd, and the marker."""
        import re
        import shlex

        lines = script.split("\n")
        cwd = shlex.split(lines[0].split("2>/dev/null")[0].replace("cd --", "").strip())[0]
        env = {}
        out = ""
        for line in lines[1:]:
            if line.startswith("export "):
                k, _, v = line[7:].partition("=")
                env[k] = shlex.split(v)[0] if v else ""
            elif line.startswith("__hornet_rc") or line.startswith("exit "):
                continue
            elif line.startswith("printf"):
                nonce = re.search(r"HORNET:([0-9a-f]+)", line).group(1)
                env_blob = "".join(f"{k}={v}\0" for k, v in env.items())
                out += f"\0HORNET:{nonce}\0{rt.get('rc', 0)}\0{cwd}\0{env_blob}"
            else:
                for cmd in (c.strip() for c in line.split(";")):
                    if cmd.startswith("cd "):
                        target = cmd[3:].strip()
                        cwd = target if target.startswith("/") else (cwd.rstrip("/") + "/" + target)
                    elif cmd.startswith("export "):
                        k, _, v = cmd[7:].partition("=")
                        env[k] = v
                    elif cmd == "pwd":
                        out += cwd + "\n"
                    elif cmd.startswith("echo "):
                        out += re.sub(r"\$(\w+)", lambda m: env.get(m.group(1), ""), cmd[5:]) + "\n"
                    elif cmd.startswith("touch "):
                        rt["files"][cmd[6:]] = 0
        return ExecResult(rt.get("rc", 0), out, "", False, False, 3)

    def start_services(self, runtime_id, spec, policy):
        rt = self.runtimes[runtime_id]
        rt["events"], rt["started"] = [], bool(spec.extra_hosts)
        return "started" if spec.extra_hosts else "none"

    def collect_service_events(self, runtime_id, spec, offset):
        evs = self.runtimes.get(runtime_id, {}).get("events", [])
        return list(evs[offset:]), len(evs)

    def snapshot_fs(self, runtime_id, spec, policy):
        return [FsEntry(path=p, kind="f", size=s, mtime=1.0) for p, s in self.runtimes[runtime_id]["files"].items()]

    def processes(self, runtime_id):
        return [{"PID": str(i)} for i in range(self.runtimes.get(runtime_id, {}).get("procs", 1))]

    def is_running(self, runtime_id):
        return self.runtimes.get(runtime_id, {}).get("alive", False)

    def destroy(self, runtime_id):
        self.destroyed.append(runtime_id)
        self.runtimes.pop(runtime_id, None)

    def list_managed(self):
        return list(self.runtimes)
