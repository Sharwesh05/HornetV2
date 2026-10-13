"""Login policy and host key for the attacker-facing SSH gateway."""
import hashlib
import os
from pathlib import Path

import asyncssh


class AuthPolicy:
    """A honeypot lets people in so it can watch them. The classic behaviour: refuse the first attempts of a
    connection, then accept. Common weak passwords are accepted straight away."""

    def __init__(self, accept_after: int = 3, weak: set[str] | None = None, log_passwords: bool = True):
        self.accept_after = accept_after
        self.weak = weak or set()
        self.log_passwords = log_passwords

    def accepts(self, attempt_number: int, password: str) -> bool:
        return password in self.weak or attempt_number >= self.accept_after

    def describe(self, password: str) -> dict:
        """What to store about an attempted password. Attacker-supplied text: bounded, and optionally reduced
        to length + a short hash so no password text is kept."""
        if self.log_passwords:
            return {"password": password[:128]}
        return {"password_len": len(password), "password_sha256": hashlib.sha256(password.encode("utf-8", "replace")).hexdigest()[:16]}


def ensure_host_key(path: str) -> str:
    """Create the server host key on first start and reuse it afterwards, so the fingerprint attackers (and
    scanners) see is stable across restarts, like a real server's."""
    p = Path(path)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        key = asyncssh.generate_private_key("ssh-ed25519", comment="")
        key.write_private_key(str(p))
        try:
            os.chmod(p, 0o600)
        except OSError:  # best effort on Windows
            pass
    return str(p)
