"""Database and NetBox client initialization for the FastAPI app."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import OperationalError
from sqlmodel import select

from proxbox_api.database import NetBoxEndpoint, create_db_and_tables, get_session
from proxbox_api.logger import logger
from proxbox_api.netbox_compat import NetBoxBase
from proxbox_api.session.netbox import get_netbox_session

if TYPE_CHECKING:
    from sqlmodel import Session

# Populated by init_database_and_netbox(); used by WebSocket handlers and helpers.
netbox_session: Any | None = None
database_session: Session | None = None
netbox_endpoints: list[Any] = []
init_ok: bool = False
last_init_error: str | None = None


def init_database_and_netbox() -> None:
    """Create tables if needed, open a DB session, and configure the default NetBox client."""
    global netbox_session, database_session, netbox_endpoints, init_ok, last_init_error

    init_ok = False
    last_init_error = None
    netbox_session = None
    database_session = None
    netbox_endpoints = []
    NetBoxBase.nb = None

    try:
        create_db_and_tables()
        database_session = next(get_session())
        netbox_session = get_netbox_session(database_session=database_session)
        NetBoxBase.nb = netbox_session
        init_ok = True
    except Exception as error:  # noqa: BLE001
        last_init_error = str(error)
        logger.exception("Database or NetBox client bootstrap failed")
        netbox_session = None
        NetBoxBase.nb = None

    if database_session:
        try:
            netbox_endpoints = database_session.exec(select(NetBoxEndpoint)).all()
        except OperationalError:
            try:
                create_db_and_tables()
                netbox_endpoints = database_session.exec(select(NetBoxEndpoint)).all()
            except Exception as error:  # noqa: BLE001
                logger.exception("Failed to load NetBox endpoint rows after schema retry")
                netbox_endpoints = []
                last_init_error = last_init_error or str(error)
