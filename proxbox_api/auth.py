"""Authentication and authorization middleware for proxbox-api."""

from __future__ import annotations

import os
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

PROXBOX_API_KEY_NAME = "X-Proxbox-API-Key"

_api_key_header = APIKeyHeader(name=PROXBOX_API_KEY_NAME, auto_error=False)


def get_hashed_api_key() -> str | None:
    """Get the configured API key hash from environment.

    The API key should be set via PROXBOX_API_KEY environment variable.
    The value is hashed using SHA-256 before storage/comparison.
    """
    raw_key = os.environ.get("PROXBOX_API_KEY", "").strip()
    if not raw_key:
        return None
    import hashlib

    return hashlib.sha256(raw_key.encode()).hexdigest()


def verify_api_key(unverified_key: str | None) -> bool:
    """Verify an API key against the configured hash."""
    if not unverified_key:
        return False
    stored_hash = get_hashed_api_key()
    if not stored_hash:
        return False
    import hashlib

    return secrets.compare_digest(hashlib.sha256(unverified_key.encode()).hexdigest(), stored_hash)


async def verify_api_key_dependency(
    request: Request,
    api_key: Annotated[str | None, Security(_api_key_header)] = None,
) -> str:
    """FastAPI dependency that validates the API key.

    Raises HTTPException 401 if:
    - No API key is configured (authentication is disabled - dev mode)
    - No API key is provided in the request
    - The provided API key is invalid

    For development/testing, if PROXBOX_API_KEY is not set, authentication
    is bypassed. In production, ensure PROXBOX_API_KEY is set.
    """
    stored_hash = get_hashed_api_key()

    if stored_hash is None:
        return "dev-mode-no-auth"

    if api_key is None:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Provide X-Proxbox-API-Key header.",
        )

    if not verify_api_key(api_key):
        raise HTTPException(
            status_code=401,
            detail="Invalid API key.",
        )

    return api_key


ApiKeyDep = Annotated[str, Depends(verify_api_key)]


def is_auth_enabled() -> bool:
    """Check if API authentication is enabled."""
    return get_hashed_api_key() is not None


class AuthenticationChecker:
    """Dependency that optionally skips auth in development mode."""

    async def __call__(
        self,
        request: Request,
        api_key: Annotated[str | None, Security(_api_key_header)] = None,
    ) -> str | None:
        """Validate API key if authentication is enabled."""
        stored_hash = get_hashed_api_key()

        if stored_hash is None:
            return None

        if api_key is None:
            raise HTTPException(
                status_code=401,
                detail="Missing API key. Provide X-Proxbox-API-Key header.",
            )

        if not verify_api_key(api_key):
            raise HTTPException(
                status_code=401,
                detail="Invalid API key.",
            )

        return api_key


OptionalAuthDep = Annotated[str | None, Depends(AuthenticationChecker())]
