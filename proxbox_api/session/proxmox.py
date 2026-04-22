"""Proxmox session management and dependency provider utilities."""

from __future__ import annotations

import os

from proxmox_sdk import ProxmoxSDK

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
    """Compatibility adapter that returns an async proxmox-sdk SDK client.

    Automatically uses mock backend when:
    - PROXMOX_API_MODE=mock environment variable is set
    - PYTEST_CURRENT_TEST is detected (pytest running)
    - TESTING=1 environment variable is set

    The SDK reads connection-tuning env vars automatically (no explicit forwarding needed):
    - PROXMOX_API_TIMEOUT: total request timeout in seconds (default: 5)
    - PROXMOX_API_CONNECT_TIMEOUT: TCP connection timeout (default: unset)
    - PROXMOX_API_RETRIES: GET/HEAD retry count on 502/503/504 (default: 0)
    - PROXMOX_API_RETRY_BACKOFF: exponential backoff base in seconds (default: 0.5)
    - HTTP_PROXY / HTTPS_PROXY: proxy URLs

    Args:
        host: Proxmox host URL
        backend: Override backend selection. If None, auto-detects mock mode.
        **kwargs: Additional arguments passed to ProxmoxSDK (e.g. connect_timeout, max_retries)

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
