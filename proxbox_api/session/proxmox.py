"""Proxmox session management and dependency provider utilities."""

from __future__ import annotations

from proxmox_openapi import ProxmoxSDK

from proxbox_api.session.proxmox_core import ProxmoxSession
from proxbox_api.session.proxmox_providers import (
    ProxmoxSessionsDep,
    load_proxmox_session_schemas,
    proxmox_sessions,
    resolve_proxmox_target_session,
)


def ProxmoxAPI(host: str, **kwargs: object) -> ProxmoxSDK:
    """Compatibility adapter that returns an async proxmox-openapi SDK client."""

    return ProxmoxSDK(host=host, backend="https", **kwargs)

__all__ = (
    "ProxmoxAPI",
    "ProxmoxSession",
    "ProxmoxSessionsDep",
    "load_proxmox_session_schemas",
    "proxmox_sessions",
    "resolve_proxmox_target_session",
)
