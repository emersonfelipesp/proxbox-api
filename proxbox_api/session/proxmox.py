"""Proxmox session management and dependency provider utilities."""

from __future__ import annotations

import os

from proxmox_openapi import ProxmoxSDK

from proxbox_api.session.proxmox_core import ProxmoxSession
from proxbox_api.session.proxmox_providers import (
    ProxmoxSessionsDep,
    close_proxmox_sessions,
    load_proxmox_session_schemas,
    proxmox_sessions,
    resolve_proxmox_target_session,
)


def _should_use_mock() -> bool:
    """Detect if we should use mock mode based on environment.

    Returns True if:
    - PROXMOX_API_MODE=mock is set
    - PYTEST_CURRENT_TEST is set (auto-detected by pytest)
    - Running in test environment (TESTING env var)
    """
    if os.getenv("PROXMOX_API_MODE") == "mock":
        return True
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    if os.getenv("TESTING") == "1":
        return True
    return False


def ProxmoxAPI(host: str, backend: str | None = None, **kwargs: object) -> ProxmoxSDK:
    """Compatibility adapter that returns an async proxmox-openapi SDK client.

    Automatically uses mock backend when:
    - PROXMOX_API_MODE=mock environment variable is set
    - PYTEST_CURRENT_TEST is detected (pytest running)
    - TESTING=1 environment variable is set

    Args:
        host: Proxmox host URL
        backend: Override backend selection. If None, auto-detects mock mode.
        **kwargs: Additional arguments passed to ProxmoxSDK

    Returns:
        ProxmoxSDK instance in mock or real mode
    """
    if backend is None and _should_use_mock():
        backend = "mock"
    return ProxmoxSDK(host=host, backend=backend or "https", **kwargs)


__all__ = (
    "ProxmoxAPI",
    "ProxmoxSession",
    "ProxmoxSessionsDep",
    "close_proxmox_sessions",
    "load_proxmox_session_schemas",
    "proxmox_sessions",
    "resolve_proxmox_target_session",
)
