"""FastAPI dependencies and Proxmox endpoint schema loading."""

from __future__ import annotations

import asyncio
import inspect
from json import JSONDecodeError
from typing import Annotated

from fastapi import Depends, Query
from sqlmodel import Session, select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.database import ProxmoxEndpoint, get_async_session
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_list_async
from proxbox_api.schemas.proxmox import ProxmoxSessionSchema, ProxmoxTokenSchema
from proxbox_api.session.netbox import get_netbox_async_session
from proxbox_api.session.proxmox_core import ProxmoxSession
from proxbox_api.settings_client import get_settings


async def proxmox_sessions(  # noqa: C901
    database_session: AsyncSession = Depends(get_async_session),
    source: Annotated[
        str,
        Query(
            title="Proxmox Endpoint Source",
            description="Source of configured Proxmox endpoints (database or netbox).",
        ),
    ] = "database",
    name: Annotated[
        str | None,
        Query(
            title="Proxmox Name",
            description="Name of Proxmox Cluster or Proxmox Node (if standalone).",
            max_length=255,
        ),
    ] = None,
    domain: Annotated[
        str | None,
        Query(
            title="Proxmox Domain",
            description="Domain of Proxmox Cluster or Proxmox Node (if standalone).",
            max_length=255,
        ),
    ] = None,
    ip_address: Annotated[
        str | None,
        Query(
            title="Proxmox IP Address",
            description="IP Address of Proxmox Cluster or Proxmox Node (if standalone).",
            max_length=45,
        ),
    ] = None,
    port: Annotated[
        int,
        Query(
            title="Proxmox HTTP Port",
            description="HTTP Port of Proxmox Cluster or Proxmox Node (if standalone).",
            ge=1,
            le=65535,
        ),
    ] = 8006,
    endpoint_ids: Annotated[
        str | None,
        Query(
            title="Proxmox Endpoint IDs",
            description="Comma-separated list of Proxmox endpoint database IDs to filter by.",
            max_length=255,
        ),
    ] = None,
):
    """
    Default Behavior: Instantiate Proxmox Sessions and return a list of Proxmox Sessions objects.
    If 'name' is provided, return only the Proxmox Session with that name.
    If 'endpoint_ids' is provided, filter by those database IDs.
    """

    if source not in ("database", "netbox"):
        raise ProxboxException(
            message="Invalid source parameter",
            detail="source must be 'database' or 'netbox'.",
        )

    endpoint_id_list = None
    if endpoint_ids is not None and endpoint_ids.strip():
        if len(endpoint_ids) > 255:
            raise ProxboxException(
                message="Invalid Proxmox endpoint_ids query parameter",
                detail="endpoint_ids exceeds maximum length.",
            )
        try:
            parts = [p.strip() for p in endpoint_ids.split(",") if p.strip()]
            if len(parts) > 100:
                raise ProxboxException(
                    message="Invalid Proxmox endpoint_ids query parameter",
                    detail="Too many endpoint IDs specified.",
                )
            endpoint_id_list = [int(eid) for eid in parts]
        except ValueError as error:
            raise ProxboxException(
                message="Invalid Proxmox endpoint_ids query parameter",
                detail="endpoint_ids must be a comma-separated list of integers.",
                python_exception=str(error),
            ) from error

    proxmox_schemas = await load_proxmox_session_schemas(
        database_session=database_session,
        source=source,
        endpoint_ids=endpoint_id_list,
    )

    async def return_single_session(field: str, value: str) -> list[ProxmoxSession]:
        for proxmox_schema in proxmox_schemas:
            if value == getattr(proxmox_schema, field, None):
                session = await ProxmoxSession.create(proxmox_schema)
                return [session]

        raise ProxboxException(
            message=f"No result found for Proxmox Sessions based on the provided {field}",
            detail="Check if the provided parameters are correct",
        )

    try:
        if ip_address is not None:
            return await return_single_session("ip_address", ip_address)

        if domain is not None:
            return await return_single_session("domain", domain)

        if name is not None:
            return await return_single_session("name", name)
    except ProxboxException as error:
        raise error

    try:
        sessions = await asyncio.gather(
            *[ProxmoxSession.create(px_schema) for px_schema in proxmox_schemas]
        )
        return list(sessions)
    except Exception as error:
        raise ProxboxException(
            message="Could not return Proxmox Sessions", python_exception=f"{error}"
        )


async def proxmox_sessions_dep(
    sessions: Annotated[list[ProxmoxSession], Depends(proxmox_sessions)],
):
    try:
        yield sessions
    finally:
        for session in sessions:
            close_method = getattr(session, "aclose", None)
            if callable(close_method):
                try:
                    await close_method()
                except Exception as error:  # pragma: no cover
                    logger.debug("Failed to clean up proxmox session: %s", error)


async def close_proxmox_sessions(pxs: list[ProxmoxSession]) -> None:
    """Best-effort cleanup for Proxmox sessions after endpoint use.

    This helper is used by routes that receive ``pxs`` from dependency injection
    and want explicit teardown at the end of execution. It is idempotent and
    tolerates already-closed sessions.
    """
    for session in pxs:
        close_method = getattr(session, "aclose", None)
        if callable(close_method):
            try:
                await close_method()
            except Exception as error:  # pragma: no cover
                logger.debug("Failed to clean up proxmox session: %s", error)


ProxmoxSessionsDep = Annotated[list[ProxmoxSession], Depends(proxmox_sessions_dep)]


def _netbox_field(endpoint: object, field: str, default: object = None) -> object:
    if isinstance(endpoint, dict):
        return endpoint.get(field, default)
    return getattr(endpoint, field, default)


def _parse_db_endpoint(endpoint: ProxmoxEndpoint) -> ProxmoxSessionSchema:
    return ProxmoxSessionSchema(
        name=endpoint.name,
        ip_address=endpoint.ip_address,
        domain=endpoint.domain,
        http_port=endpoint.port,
        user=endpoint.username,
        password=endpoint.get_decrypted_password(),
        ssl=endpoint.verify_ssl,
        token=ProxmoxTokenSchema(
            name=endpoint.token_name,
            value=endpoint.get_decrypted_token_value(),
        ),
        timeout=endpoint.timeout,
        max_retries=endpoint.max_retries,
        retry_backoff=float(endpoint.retry_backoff) if endpoint.retry_backoff is not None else None,
    )


def _parse_netbox_endpoint(
    endpoint: object,
    plugin_settings: dict[str, object] | None = None,
) -> ProxmoxSessionSchema:
    ip = None
    ip_address_object = _netbox_field(endpoint, "ip_address")
    if ip_address_object:
        if isinstance(ip_address_object, dict):
            ip_address_with_mask = ip_address_object.get("address")
        else:
            ip_address_with_mask = getattr(ip_address_object, "address", None)
        if ip_address_with_mask:
            ip = ip_address_with_mask.split("/")[0]

    settings = plugin_settings or {}
    raw_timeout = _netbox_field(endpoint, "timeout")
    raw_max_retries = _netbox_field(endpoint, "max_retries")
    raw_retry_backoff = _netbox_field(endpoint, "retry_backoff")

    return ProxmoxSessionSchema(
        name=_netbox_field(endpoint, "name"),
        ip_address=ip,
        domain=_netbox_field(endpoint, "domain"),
        http_port=_netbox_field(endpoint, "port"),
        user=_netbox_field(endpoint, "username"),
        password=_netbox_field(endpoint, "password"),
        ssl=bool(_netbox_field(endpoint, "verify_ssl", False)),
        token=ProxmoxTokenSchema(
            name=_netbox_field(endpoint, "token_name"),
            value=_netbox_field(endpoint, "token_value"),
        ),
        timeout=int(raw_timeout) if raw_timeout is not None else settings.get("proxmox_timeout"),  # type: ignore[arg-type]
        max_retries=int(raw_max_retries)
        if raw_max_retries is not None
        else settings.get("proxmox_max_retries"),  # type: ignore[arg-type]
        retry_backoff=float(raw_retry_backoff)
        if raw_retry_backoff is not None
        else settings.get("proxmox_retry_backoff"),  # type: ignore[arg-type]
    )


async def load_proxmox_session_schemas(
    database_session: AsyncSession | Session,
    source: str = "database",
    endpoint_ids: list[int] | None = None,
) -> list[ProxmoxSessionSchema]:
    """Load configured Proxmox endpoint schemas without creating Proxmox API sessions."""

    if source == "netbox":
        netbox_session = get_netbox_async_session(database_session=database_session)
        if inspect.isawaitable(netbox_session):
            netbox_session = await netbox_session

        plugin_settings = get_settings(netbox_session=netbox_session)

        try:
            url = "/api/plugins/proxbox/endpoints/proxmox/"
            if endpoint_ids:
                ids_param = ",".join(str(eid) for eid in endpoint_ids)
                url = f"{url}?id={ids_param}"
            netbox_endpoints = await rest_list_async(
                netbox_session,
                url,
            )
        except JSONDecodeError as error:
            raise ProxboxException(
                message="NetBox returned invalid JSON while fetching Proxmox endpoints",
                python_exception=str(error),
            )
        return [_parse_netbox_endpoint(endpoint, plugin_settings) for endpoint in netbox_endpoints]

    query = select(ProxmoxEndpoint)
    if endpoint_ids:
        query = query.where(ProxmoxEndpoint.id.in_(endpoint_ids))
    result = database_session.exec(query)
    if inspect.isawaitable(result):
        result = await result
    db_endpoints = result.all()
    return [_parse_db_endpoint(endpoint) for endpoint in db_endpoints]


async def resolve_proxmox_target_session(
    database_session: AsyncSession | Session,
    *,
    source: str = "database",
    name: str | None = None,
    domain: str | None = None,
    ip_address: str | None = None,
) -> ProxmoxSession:
    """Resolve a single Proxmox target for generated live proxy routes."""

    proxmox_schemas = await load_proxmox_session_schemas(
        database_session=database_session,
        source=source,
    )

    selectors = (
        ("ip_address", ip_address),
        ("domain", domain),
        ("name", name),
    )
    for field, value in selectors:
        if value is None:
            continue
        for proxmox_schema in proxmox_schemas:
            if value == getattr(proxmox_schema, field, None):
                return await ProxmoxSession.create(proxmox_schema)
        raise ProxboxException(
            message=f"No result found for Proxmox Sessions based on the provided {field}",
            detail="Check if the provided parameters are correct",
        )

    if not proxmox_schemas:
        raise ProxboxException(
            message="No Proxmox endpoints found for generated proxy route.",
            detail="Configure at least one Proxmox endpoint before using generated proxy routes.",
        )

    if len(proxmox_schemas) > 1:
        raise ProxboxException(
            message="Multiple Proxmox endpoints configured; provide name, domain, or ip_address.",
            detail="Generated Proxmox proxy routes require an explicit target when more than one endpoint is configured.",
        )

    return await ProxmoxSession.create(proxmox_schemas[0])
