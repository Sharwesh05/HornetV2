import secrets

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.config import Settings, get_settings

ACTOR = "admin-api-key"

# Declared as a security scheme so /docs shows the Authorize button; our own 401 stays in charge.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_admin(
    x_api_key: str | None = Security(api_key_header),
    settings: Settings = Depends(get_settings),
) -> str:
    """Admin API authentication. Constant-time comparison; the attacker-facing gateway never
    shares this surface."""
    if not x_api_key or not secrets.compare_digest(
        x_api_key.encode(), settings.admin_api_key.encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key")
    return ACTOR
