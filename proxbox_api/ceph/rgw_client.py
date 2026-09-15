"""RGW Admin Ops provider client bridge for Ceph v2 (#97 / #435).

Defensive bridge to the ``proxmox-sdk`` RGW Admin Ops client (added in 0.0.11),
mirroring ``dashboard_sdk_importable()``. Lets the external-cluster adapter
report RGW capability accurately and degrade cleanly on an older SDK pin.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RGWAdminConfig:
    """Connection config for an RGW Admin Ops endpoint (secrets decrypted)."""

    base_url: str
    access_key: str
    secret_key: str
    admin_path: str = "admin"
    verify_ssl: bool = True
    timeout: int = 30


def rgw_sdk_importable() -> bool:
    """Return ``True`` when the installed ``proxmox-sdk`` ships the RGW client."""
    try:
        from proxmox_sdk.ceph.providers import RGWAdminClient  # noqa: F401,PLC0415
    except Exception:  # noqa: BLE001 - any import failure means the provider is unavailable
        return False
    return True


def build_rgw_client(config: RGWAdminConfig, *, transport: Any = None) -> Any:
    """Construct an SDK ``RGWAdminClient`` from ``config`` (caller gates on import)."""
    from proxmox_sdk.ceph.providers import RGWAdminClient  # noqa: PLC0415

    return RGWAdminClient(
        config.base_url,
        access_key=config.access_key,
        secret_key=config.secret_key,
        admin_path=config.admin_path,
        verify_ssl=config.verify_ssl,
        timeout=config.timeout,
        transport=transport,
    )


async def validate_rgw_endpoint(
    config: RGWAdminConfig, *, client_factory: Any = None
) -> tuple[bool, str | None]:
    """Probe an RGW Admin Ops endpoint. Returns ``(ok, error_message)``."""
    if client_factory is None and not rgw_sdk_importable():
        return False, "proxmox-sdk>=0.0.11 is required for the RGW Admin Ops provider"
    client = client_factory(config) if client_factory else build_rgw_client(config)
    try:
        caps = await client.capabilities()
    except Exception:  # noqa: BLE001 - provider diagnostics may contain credentials
        return False, "RGW Admin endpoint validation failed."
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass
    available = bool(getattr(caps, "available", False))
    return available, (None if available else "RGW Admin endpoint is unavailable.")
