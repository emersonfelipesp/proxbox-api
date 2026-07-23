"""Ceph Dashboard provider client bridge for Ceph v2 (#98).

Wraps the ``proxmox-sdk`` direct Ceph Dashboard client behind a defensive
import, mirroring the ``cephwrite_importable()`` pattern: the adapter degrades
cleanly (reports ``available=False``) when the installed ``proxmox-sdk`` pin
does not yet ship ``proxmox_sdk.ceph.providers`` (added in 0.0.11), instead of
silently failing. Pin proxmox-sdk to >=0.0.11 to activate the Dashboard path.

The SDK client owns its own HTTP session (the Dashboard API is a standalone
service, not the PVE API), so no Proxmox endpoint is required — this is what
lets the Dashboard provider serve external/standalone Ceph clusters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DashboardEndpointConfig:
    """Connection config for a Ceph Dashboard endpoint (secrets decrypted)."""

    base_url: str
    username: str | None = None
    password: str | None = None
    token: str | None = None
    verify_ssl: bool = True
    api_version: str = "1.0"
    timeout: int = 30


def dashboard_sdk_importable() -> bool:
    """Return ``True`` when the installed ``proxmox-sdk`` ships the Dashboard client."""
    try:
        from proxmox_sdk.ceph.providers import DashboardCephClient  # noqa: F401,PLC0415
    except Exception:  # noqa: BLE001 - any import failure means the provider is unavailable
        return False
    return True


def build_dashboard_client(config: DashboardEndpointConfig, *, transport: Any = None) -> Any:
    """Construct an SDK ``DashboardCephClient`` from ``config``.

    Raises ``ImportError`` if the SDK Dashboard client is not importable; callers
    gate on :func:`dashboard_sdk_importable` first.
    """
    from proxmox_sdk.ceph.providers import DashboardCephClient  # noqa: PLC0415

    return DashboardCephClient(
        config.base_url,
        username=config.username,
        password=config.password,
        token=config.token,
        verify_ssl=config.verify_ssl,
        api_version=config.api_version,
        timeout=config.timeout,
        transport=transport,
    )


async def validate_dashboard_endpoint(
    config: DashboardEndpointConfig, *, client_factory: Any = None
) -> tuple[bool, str | None]:
    """Probe a Ceph Dashboard endpoint. Returns ``(ok, error_message)``.

    Returns ``(False, ...)`` when the SDK Dashboard client is not importable
    (older proxmox-sdk pin) rather than raising.
    """
    if client_factory is None and not dashboard_sdk_importable():
        return False, "proxmox-sdk>=0.0.11 is required for the Ceph Dashboard provider"
    client = client_factory(config) if client_factory else build_dashboard_client(config)
    try:
        caps = await client.capabilities()
    except Exception:  # noqa: BLE001 - provider diagnostics may contain credentials
        return False, "Ceph Dashboard endpoint validation failed."
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
    available = bool(getattr(caps, "available", False))
    return available, (None if available else "Ceph Dashboard endpoint is unavailable.")
