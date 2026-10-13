"""Decide what a service is from its (sanitized) image name, ports and command line. Pure function, no I/O.

Roles are the extension point for supporting any kind of server: add a role here and a builder in
roles.py. Anything unrecognised becomes 'generic' and still gets a sensible twin. Detection never
relies on one fixed port: a service is an HTTP app if its command line says so, if it uses a usual web
port, or if it publishes a port that is not a well-known non-HTTP protocol.
"""
import re

HTTP_PORTS = {80, 443, 3000, 5000, 8000, 8080, 8443, 8888, 9000}
# ports that belong to well-known non-HTTP protocols (ssh, smtp, dns, databases, brokers ...)
NON_HTTP_PORTS = {21, 22, 23, 25, 53, 110, 143, 389, 465, 587, 636, 993, 995, 1433, 1521, 3306, 4222, 5432, 5671,
                  5672, 6379, 9092, 11211, 27017}

_IMAGE_ROLES = (
    ("postgres", re.compile(r"(^|/)(postgres|postgresql|timescaledb|postgis)([:/-]|$)")),
    ("mysql", re.compile(r"(^|/)(mysql|mariadb|percona)([:/-]|$)")),
    ("redis", re.compile(r"(^|/)(redis|valkey|keydb)([:/-]|$)")),
    ("nginx", re.compile(r"(^|/)(nginx|caddy|traefik|haproxy|httpd|apache)([:/-]|$)")),
)
_HTTP_COMMAND = re.compile(
    r"http|serve|gunicorn|uvicorn|hypercorn|flask|django|runserver|rails|puma|php-fpm|express|npm|node|next|"
    r"spring|tomcat|jetty|fastapi|webpack|vite", re.I
)


def detect_role(image: str, container_ports: list[int], command_text: str = "", published: bool = False) -> str:
    img = image.lower()
    for role, pattern in _IMAGE_ROLES:
        if pattern.search(img):
            return role
    ports = set(container_ports)
    if ports & HTTP_PORTS or _HTTP_COMMAND.search(command_text):
        return "http-app"
    if published and ports and not ports <= NON_HTTP_PORTS:
        return "http-app"
    return "generic"
