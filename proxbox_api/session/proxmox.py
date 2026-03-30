"""Proxmox session management and dependency provider utilities."""

from __future__ import annotations

from proxmoxer import ProxmoxAPI

from proxbox_api.session.proxmox_core import ProxmoxSession
from proxbox_api.session.proxmox_providers import (
    ProxmoxSessionsDep,
    load_proxmox_session_schemas,
    proxmox_sessions,
    resolve_proxmox_target_session,
)

__all__ = (
    "ProxmoxAPI",
    "ProxmoxSession",
    "ProxmoxSessionsDep",
    "load_proxmox_session_schemas",
    "proxmox_sessions",
    "resolve_proxmox_target_session",
)
