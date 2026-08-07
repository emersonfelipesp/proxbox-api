"""CORS allow-origins list from NetBox endpoint records and environment."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp


def build_cors_origins(netbox_endpoints: Sequence[object]) -> list[str]:
    """Return unique allowed origins for CORSMiddleware."""
    origins: list[str] = []
    for netbox_endpoint in netbox_endpoints:
        # Protocol matches the endpoint.url logic: port 443 or verify_ssl=True → https.
        # verify_ssl=False means "skip cert check" for proxbox-api's OWN client, not
        # that the endpoint itself uses plain HTTP.
        port = int(getattr(netbox_endpoint, "port", 443) or 443)
        verify_ssl = bool(getattr(netbox_endpoint, "verify_ssl", True))
        protocol = "https" if port == 443 or verify_ssl else "http"
        domain = getattr(netbox_endpoint, "domain", None)
        if not domain:
            continue
        origins.extend(
            [
                f"{protocol}://{domain}",
                f"{protocol}://{domain}:80",
                f"{protocol}://{domain}:443",
                f"{protocol}://{domain}:8000",
            ]
        )

    origins.extend(
        [
            "https://127.0.0.1:443",
            "http://127.0.0.1:80",
            "http://127.0.0.1:8000",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:3001",
            "http://127.0.0.1:3001",
        ]
    )

    for part in os.environ.get("PROXBOX_CORS_EXTRA_ORIGINS", "").split(","):
        origin = part.strip().rstrip("/")
        if origin:
            origins.append(origin)

    return list(dict.fromkeys(origins))


class DatabaseAwareCORSMiddleware(CORSMiddleware):
    """Preserve endpoint-derived origins after DB bootstrap moved to lifespan."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        endpoint_provider: Callable[[], Sequence[object]],
        allow_origins: Sequence[str] = (),
        allow_methods: Sequence[str] = ("GET",),
        allow_headers: Sequence[str] = (),
        allow_credentials: bool = False,
        allow_origin_regex: str | None = None,
        allow_private_network: bool = False,
        expose_headers: Sequence[str] = (),
        max_age: int = 600,
    ) -> None:
        self._endpoint_provider = endpoint_provider
        super().__init__(
            app,
            allow_origins=allow_origins,
            allow_methods=allow_methods,
            allow_headers=allow_headers,
            allow_credentials=allow_credentials,
            allow_origin_regex=allow_origin_regex,
            allow_private_network=allow_private_network,
            expose_headers=expose_headers,
            max_age=max_age,
        )

    def is_allowed_origin(self, origin: str) -> bool:
        if super().is_allowed_origin(origin):
            return True
        return origin in build_cors_origins(self._endpoint_provider())
