"""E2E testing utilities for proxbox-api integration testing with NetBox."""

from proxbox_api.e2e.demo_auth import generate_password, generate_username
from proxbox_api.e2e.session import (
    E2E_TAG_COLOR,
    E2E_TAG_DESCRIPTION,
    E2E_TAG_NAME,
    E2E_TAG_SLUG,
    build_e2e_tag_refs,
    create_netbox_e2e_session,
    ensure_e2e_tag,
)

__all__ = [
    "generate_password",
    "generate_username",
    "E2E_TAG_COLOR",
    "E2E_TAG_DESCRIPTION",
    "E2E_TAG_NAME",
    "E2E_TAG_SLUG",
    "build_e2e_tag_refs",
    "create_netbox_e2e_session",
    "ensure_e2e_tag",
]
