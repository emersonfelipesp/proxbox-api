"""Database and NetBox client initialization for the FastAPI app."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select

from proxbox_api.constants import DEFAULT_LOG_PATH
from proxbox_api.database import (
    DatabaseConfigurationError,
    DatabaseStartupError,
    NetBoxEndpoint,
    get_session,
    initialize_database_and_schema,
)
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import configure_file_logging_path, logger
from proxbox_api.netbox_compat import NetBoxBase
from proxbox_api.session.netbox import netbox_api_from_endpoint
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
        target = initialize_database_and_schema()
        database_session = next(get_session())
        logger.info(
            "SQLite database startup verification passed",
            extra={
                "database_path": str(target.path),
                "configuration_source": target.source,
                "startup_lock_path": str(target.startup_lock_path),
            },
        )
    except (DatabaseConfigurationError, DatabaseStartupError) as error:
        last_init_error = str(error)
        logger.error("bootstrap: fatal database configuration or verification error: %s", error)
        raise
    except (OSError, SQLAlchemyError) as error:
        startup_error = DatabaseStartupError(
            "Configured SQLite database failed schema initialization. "
            "Check database ownership, permissions, available space, and integrity."
        )
        last_init_error = str(startup_error)
        logger.exception("bootstrap: fatal database schema initialization error")
        raise startup_error from error

    try:
        netbox_endpoints = list(
            database_session.exec(select(NetBoxEndpoint).order_by(NetBoxEndpoint.id)).all()
        )
    except Exception as error:  # noqa: BLE001
        startup_error = DatabaseStartupError(
            "Configured SQLite database failed the required post-schema endpoint read. "
            "Check database integrity, ownership, available space, and migration logs."
        )
        last_init_error = str(startup_error)
        logger.exception("bootstrap: fatal database endpoint-table read failure")
        raise startup_error from error
    finally:
        database_session.close()
        database_session = None

    try:
        skip_netbox = os.environ.get("PROXBOX_SKIP_NETBOX_BOOTSTRAP", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        enabled_endpoints = [endpoint for endpoint in netbox_endpoints if endpoint.enabled]
        if skip_netbox:
            logger.info(
                "Skipping NetBox API bootstrap (PROXBOX_SKIP_NETBOX_BOOTSTRAP); "
                "no default NetBox client until an endpoint is configured"
            )
        elif enabled_endpoints:
            netbox_session = netbox_api_from_endpoint(enabled_endpoints[0])
            NetBoxBase.nb = netbox_session
        else:
            last_init_error = "No enabled NetBox endpoint found"
            logger.warning("bootstrap: NetBox is not connected — %s", last_init_error)
        init_ok = True
    except ProxboxException as error:
        last_init_error = str(error)
        logger.warning("bootstrap: NetBox is not connected — %s", error)
        netbox_session = None
        NetBoxBase.nb = None
        init_ok = True  # Required database reads passed; NetBox configuration is optional.
    except Exception as error:  # noqa: BLE001
        last_init_error = str(error)
        logger.exception("bootstrap: NetBox client bootstrap failed")
        netbox_session = None
        NetBoxBase.nb = None

    _configure_backend_file_logging()
