"""Helpers for obtaining a NetBox API session outside FastAPI dependencies."""

from __future__ import annotations

from contextlib import closing

from proxbox_api.database import get_session
from proxbox_api.logger import logger
from proxbox_api.session.netbox import get_netbox_session


def get_raw_netbox_session() -> object | None:
    """Return a NetBox session using a fresh DB session (same shape as dependency-injected session)."""
    try:
        with closing(get_session()) as session_iter:
            database_session = next(session_iter)
            return get_netbox_session(database_session)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to build NetBox session for raw helper")
        return None
