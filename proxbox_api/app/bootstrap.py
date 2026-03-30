"""Database and NetBox client initialization for the FastAPI app."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import OperationalError
from sqlmodel import select

from proxbox_api.database import NetBoxEndpoint, create_db_and_tables, get_session
from proxbox_api.netbox_compat import NetBoxBase
from proxbox_api.session.netbox import get_netbox_session

if TYPE_CHECKING:
    from sqlmodel import Session

# Populated by init_database_and_netbox(); used by WebSocket handlers and helpers.
netbox_session: Any | None = None
database_session: Session | None = None
netbox_endpoints: list[Any] = []


def init_database_and_netbox() -> None:
    """Create tables if needed, open a DB session, and configure the default NetBox client."""
    global netbox_session, database_session, netbox_endpoints

    try:
        create_db_and_tables()
        database_session = next(get_session())
        netbox_session = get_netbox_session(database_session=database_session)
        NetBoxBase.nb = netbox_session
    except Exception as error:  # noqa: BLE001
        print(error)

    if database_session:
        try:
            netbox_endpoints = database_session.exec(select(NetBoxEndpoint)).all()
        except OperationalError:
            create_db_and_tables()
            netbox_endpoints = database_session.exec(select(NetBoxEndpoint)).all()
