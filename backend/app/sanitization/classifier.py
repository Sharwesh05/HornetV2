import re

_SECRET = re.compile(
    r"(PASS|PWD|SECRET|TOKEN|KEY|CRED|AUTH|PRIVATE|CERT|SALT|SIGNING|DSN|STRIPE|SESSION)", re.I
)
_CONNECTION = re.compile(r"(DATABASE|DB|REDIS|CACHE|BROKER|AMQP|MONGO|MYSQL|POSTGRES|SQL).*(URL|URI|DSN|CONN)|^(URL|URI)$", re.I)


def classify_env_name(name: str) -> str:
    """secret | connection | config, judged from the variable name only (values are never read)."""
    if _CONNECTION.search(name):
        return "connection"
    if _SECRET.search(name):
        return "secret"
    return "config"
