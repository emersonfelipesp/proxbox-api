"""Proxmox session management and dependency provider utilities."""

from __future__ import annotations

from proxmox_openapi import ProxmoxSDK
from proxmox_openapi.sdk.sync import SyncProxmoxSDK

from proxbox_api.session.proxmox_core import ProxmoxSession
from proxbox_api.session.proxmox_providers import (
    ProxmoxSessionsDep,
    load_proxmox_session_schemas,
    proxmox_sessions,
    resolve_proxmox_target_session,
)


def ProxmoxAPI(host: str, **kwargs: object) -> SyncProxmoxSDK:
    """Compatibility adapter that returns a sync proxmox-openapi SDK client."""

    return ProxmoxSDK.sync(host=host, backend="https", **kwargs)

__all__ = (
    "ProxmoxAPI",
    "ProxmoxSession",
    "ProxmoxSessionsDep",
    "load_proxmox_session_schemas",
    "proxmox_sessions",
    "resolve_proxmox_target_session",
)
