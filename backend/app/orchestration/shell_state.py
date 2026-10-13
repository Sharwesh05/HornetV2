"""State that survives between commands of one attacker session: working directory and exported variables.

Every command still runs as its own process inside the twin (so each one is separate, recorded evidence),
but it is wrapped so the next command starts where the last one ended:

    cd <last cwd>; export <last vars>; <the attacker's command>; print marker + exit code + $PWD + env

The marker contains a random per-command nonce, is stripped from the output the attacker sees, and the
state it carries is only ever used to restore THAT attacker's own next command. A forged marker can at
worst change the attacker's own cwd/env. Values are shell-quoted when restored.
"""
import re
import secrets
import shlex
from dataclasses import dataclass, field

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SKIP = {"_", "PWD", "OLDPWD", "SHLVL", "BASH_EXECUTION_STRING", "BASHOPTS", "SHELLOPTS", "BASH_ENV", "ENV", "IFS"}
MAX_VARS = 100
MAX_VALUE = 4096
MAX_ENV_BYTES = 16 * 1024
MAX_CWD = 1024


@dataclass
class ShellState:
    cwd: str = "/home/user"
    env: dict[str, str] = field(default_factory=dict)

    def wrap(self, command: str) -> tuple[str, str]:
        """Return (script, nonce). The script is the argument for `bash -c`."""
        nonce = secrets.token_hex(8)
        lines = [f"cd -- {shlex.quote(self.cwd)} 2>/dev/null || cd ~ 2>/dev/null"]
        lines += [f"export {k}={shlex.quote(v)}" for k, v in self.env.items()]
        lines += [command, "__hornet_rc=$?",
                  f"printf '\\000HORNET:{nonce}\\000%s\\000%s\\000' \"$__hornet_rc\" \"$PWD\"; env -0",
                  "exit $__hornet_rc"]
        return "\n".join(lines), nonce

    def parse(self, stdout: str, nonce: str) -> tuple[str, int | None]:
        """Strip the marker from stdout, update this state, and return (clean stdout, exit code or None).
        None means the marker never came (the command exited the shell, was killed, or output was cut)."""
        marker = f"\x00HORNET:{nonce}\x00"
        idx = stdout.rfind(marker)
        if idx < 0:
            return stdout, None
        clean, tail = stdout[:idx], stdout[idx + len(marker):]
        parts = tail.split("\x00")
        if len(parts) < 2 or not parts[0].lstrip("-").isdigit():
            return clean, None
        rc, cwd = int(parts[0]), parts[1]
        if cwd.startswith("/") and len(cwd) <= MAX_CWD and "\n" not in cwd:
            self.cwd = cwd
        env, size = {}, 0
        for item in parts[2:]:
            name, sep, value = item.partition("=")
            if not sep or not _NAME.match(name) or name in _SKIP or name.startswith("BASH_") or len(value) > MAX_VALUE:
                continue
            size += len(name) + len(value)
            if len(env) >= MAX_VARS or size > MAX_ENV_BYTES:
                break
            env[name] = value
        self.env = env
        return clean, rc
