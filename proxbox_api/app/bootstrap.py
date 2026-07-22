"""Database and NetBox client initialization for the FastAPI app."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from sqlalchemy.exc import OperationalError
from sqlmodel import select

from proxbox_api.constants import DEFAULT_LOG_PATH
from proxbox_api.database import NetBoxEndpoint, create_db_and_tables, get_session
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import configure_file_logging_path, logger
from proxbox_api.netbox_compat import NetBoxBase
from proxbox_api.session.netbox import get_netbox_session
from proxbox_api.settings_client import get_settings

if TYPE_CHECKING:
    from netbox_sdk.facade import Api
    from sqlmodel import Session

# Populated by init_database_and_netbox(); used by WebSocket handlers and helpers.
netbox_session: Api | None = None
database_session: Session | None = None
netbox_endpoints: list[NetBoxEndpoint] = []
init_ok: bool = False
last_init_error: str | None = None


def _configure_backend_file_logging() -> None:
    """Apply file log path from Proxbox plugin settings when available."""
    try:
        settings = get_settings(netbox_session=netbox_session, use_cache=False)
        configured_path = settings.get("backend_log_file_path", DEFAULT_LOG_PATH)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to resolve backend_log_file_path from Proxbox plugin settings; using default"
        )
        configured_path = DEFAULT_LOG_PATH

    applied_path = configure_file_logging_path(configured_path)
    if applied_path:
        logger.info("Backend file logs configured", extra={"backend_log_file_path": applied_path})
        return

    logger.warning(
        "Backend file logs disabled because no log archive path could be created",
        extra={"backend_log_file_path": configured_path},
    )


def init_database_state() -> None:
    """Create tables and load endpoint metadata needed while constructing the app."""
    global database_session, netbox_endpoints, init_ok, last_init_error

    init_ok = False
    last_init_error = None
    database_session = None
    netbox_endpoints = []
    session_iterator = get_session()

    try:
        create_db_and_tables()
        database_session = next(session_iterator)
        try:
            netbox_endpoints = list(database_session.exec(select(NetBoxEndpoint)).all())
        except OperationalError:
            create_db_and_tables()
            netbox_endpoints = list(database_session.exec(select(NetBoxEndpoint)).all())
        init_ok = True
    except Exception as error:  # noqa: BLE001
        last_init_error = str(error)
        logger.exception("bootstrap: Database initialization failed")
        netbox_endpoints = []
    finally:
        if database_session is not None:
            database_session.close()
            database_session = None
        session_iterator.close()


async def init_database_and_netbox() -> None:
    """Initialize database state and asynchronously acquire the default NetBox client."""
    global netbox_session, init_ok, last_init_error

    init_database_state()
    netbox_session = None
    NetBoxBase.nb = None

    if not init_ok:
        _configure_backend_file_logging()
        return

    skip_netbox = os.environ.get("PROXBOX_SKIP_NETBOX_BOOTSTRAP", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if skip_netbox:
        logger.info(
            "Skipping NetBox API bootstrap (PROXBOX_SKIP_NETBOX_BOOTSTRAP); "
            "no default NetBox client until an endpoint is configured"
        )
        _configure_backend_file_logging()
        return

    try:
        session_iterator = get_session()
        try:
            sync_database_session = next(session_iterator)
            netbox_session = await get_netbox_session(sync_database_session)
        finally:
            session_iterator.close()
        NetBoxBase.nb = netbox_session
    except ProxboxException as error:
        last_init_error = str(error)
        logger.warning("bootstrap: NetBox is not connected — %s", error)
        netbox_session = None
        NetBoxBase.nb = None
        init_ok = True  # DB is healthy; missing NetBox endpoint is an expected state
    except Exception as error:  # noqa: BLE001
        last_init_error = str(error)
        logger.exception("bootstrap: NetBox client bootstrap failed")
        netbox_session = None
        NetBoxBase.nb = None
        init_ok = False
    _configure_backend_file_logging()
