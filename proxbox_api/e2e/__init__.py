"""E2E testing utilities for proxbox-api integration testing with NetBox demo."""

from proxbox_api.e2e.demo_auth import (
    DemoUnavailableError,
    bootstrap_demo_profile,
    create_demo_user,
    demo_auth_required,
    login,
    provision_demo_token,
    refresh_demo_profile,
)
from proxbox_api.e2e.session import create_netbox_demo_session, ensure_e2e_tag

__all__ = [
    "DemoUnavailableError",
    "bootstrap_demo_profile",
    "create_demo_user",
    "demo_auth_required",
    "login",
    "provision_demo_token",
    "refresh_demo_profile",
    "create_netbox_demo_session",
    "ensure_e2e_tag",
]
