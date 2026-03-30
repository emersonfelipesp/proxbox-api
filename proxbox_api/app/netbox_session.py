"""Helpers for obtaining a NetBox API session outside FastAPI dependencies."""

from __future__ import annotations

from typing import Any

from proxbox_api.database import get_session
from proxbox_api.session.netbox import get_netbox_session


def get_raw_netbox_session() -> Any | None:
    """Return a NetBox session using a fresh DB session (same shape as dependency-injected session)."""
    try:
        database_session = next(get_session())
        return get_netbox_session(database_session)
    except Exception as error:  # noqa: BLE001
        print(f"Error getting NetBox session: {error}")
        return None
