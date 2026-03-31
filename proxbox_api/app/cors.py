"""CORS allow-origins list from NetBox endpoint records and environment."""

from __future__ import annotations

import os
from typing import Any


def build_cors_origins(netbox_endpoints: list[Any]) -> list[str]:
    """Return unique allowed origins for CORSMiddleware."""
    origins: list[str] = []
    for netbox_endpoint in netbox_endpoints:
        protocol = "https" if netbox_endpoint.verify_ssl else "http"
        origins.extend(
            [
                f"{protocol}://{netbox_endpoint.domain}",
                f"{protocol}://{netbox_endpoint.domain}:80",
                f"{protocol}://{netbox_endpoint.domain}:443",
                f"{protocol}://{netbox_endpoint.domain}:8000",
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
