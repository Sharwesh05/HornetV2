import secrets

from fastapi import Depends, Header, HTTPException, status

from app.config import Settings, get_settings

ACTOR = "admin-api-key"


def require_admin(
    x_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> str:
    """Admin API authentication. Constant-time comparison; the attacker-facing gateway never
    shares this surface."""
    if not x_api_key or not secrets.compare_digest(
        x_api_key.encode(), settings.admin_api_key.encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key")
    return ACTOR
