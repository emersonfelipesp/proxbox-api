"""Helpers for obtaining a NetBox API session outside FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from netbox_sdk.facade import Api


def get_raw_netbox_session() -> Api | None:
    """Return the lifecycle-owned default client without constructing an unowned client."""
    from proxbox_api.app import bootstrap

    return bootstrap.netbox_session
