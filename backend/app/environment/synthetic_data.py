"""Deterministic synthetic data. Same seed + same clock => same output, so builds are reproducible.

Everything here is invented: example.test emails, documentation-range IPs (RFC 5737), random names.
"""
import random
import string
from datetime import datetime, timedelta

from app.environment.plan import Person

FIRST = ["Alice", "Bob", "Carla", "Dev", "Elena", "Farid", "Grace", "Hiro", "Ines", "Jonas", "Kofi", "Lena"]
LAST = ["Nguyen", "Okafor", "Silva", "Kowalski", "Haddad", "Moreau", "Tanaka", "Brandt", "Rossi", "Singh"]
PUBLIC_NETS = ("203.0.113", "198.51.100")  # documentation ranges: never real hosts
INTERNAL_NET = "10.20.0"


class Synth:
    def __init__(self, seed: int, now: datetime):
        self.rng = random.Random(seed)
        self.now = now

    # -- secrets (fake, but shaped like the real thing) --------------------------------------
    def _alnum(self, n: int, alphabet: str = string.ascii_letters + string.digits) -> str:
        return "".join(self.rng.choice(alphabet) for _ in range(n))

    def password(self, n: int = 20) -> str:
        return self._alnum(n)

    def token(self, n: int = 40) -> str:
        return self._alnum(n)

    def stripe_key(self) -> str:
        return "sk_test_" + self._alnum(24)

    def aws_key_id(self) -> str:
        return "AKIA" + self._alnum(16, string.ascii_uppercase + string.digits)

    # -- people and network --------------------------------------------------------------------
    def people(self, n: int) -> list[Person]:
        out: list[Person] = []
        used: set[tuple[str, str]] = set()
        while len(out) < n:
            f, last = self.rng.choice(FIRST), self.rng.choice(LAST)
            if (f, last) in used:
                continue
            used.add((f, last))
            out.append(Person(f, last, f"{f.lower()}.{last.lower()}@example.test"))
        return out

    def public_ip(self) -> str:
        return f"{self.rng.choice(PUBLIC_NETS)}.{self.rng.randint(2, 250)}"

    def internal_ip(self, index: int) -> str:
        return f"{INTERNAL_NET}.{10 + index}"

    # -- time ------------------------------------------------------------------------------
    def ts(self, minutes_ago: int) -> datetime:
        return self.now - timedelta(minutes=minutes_ago)

    def request_id(self) -> str:
        return self._alnum(10, string.ascii_lowercase + string.digits)
