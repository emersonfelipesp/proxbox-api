"""FastAPI dependencies and Proxmox endpoint schema loading."""

from __future__ import annotations

from json import JSONDecodeError
from typing import Annotated, Any

from fastapi import Depends, Query
from sqlmodel import select

from proxbox_api.database import DatabaseSessionDep, ProxmoxEndpoint
from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import rest_list_async
from proxbox_api.schemas.proxmox import ProxmoxSessionSchema, ProxmoxTokenSchema
from proxbox_api.session.netbox import get_netbox_async_session
from proxbox_api.session.proxmox_core import ProxmoxSession


async def proxmox_sessions(  # noqa: C901
    database_session: DatabaseSessionDep,
    source: str = "database",
    name: Annotated[
        str | None,
        Query(
            title="Proxmox Name",
            description="Name of Proxmox Cluster or Proxmox Node (if standalone).",
        ),
    ] = None,
    domain: Annotated[
        str | None,
        Query(
            title="Proxmox Domain",
            description="Domain of Proxmox Cluster or Proxmox Node (if standalone).",
        ),
    ] = None,
    ip_address: Annotated[
        str | None,
        Query(
            title="Proxmox IP Address",
            description="IP Address of Proxmox Cluster or Proxmox Node (if standalone).",
        ),
    ] = None,
    port: Annotated[
        int,
        Query(
            title="Proxmox HTTP Port",
            description="HTTP Port of Proxmox Cluster or Proxmox Node (if standalone).",
        ),
    ] = 8006,
    endpoint_ids: Annotated[
        str | None,
        Query(
            title="Proxmox Endpoint IDs",
            description="Comma-separated list of Proxmox endpoint database IDs to filter by.",
        ),
    ] = None,
):
    """
    Default Behavior: Instantiate Proxmox Sessions and return a list of Proxmox Sessions objects.
    If 'name' is provided, return only the Proxmox Session with that name.
    If 'endpoint_ids' is provided, filter by those database IDs.
    """

    endpoint_id_list = None
    if endpoint_ids is not None and endpoint_ids.strip():
        try:
            endpoint_id_list = [int(eid.strip()) for eid in endpoint_ids.split(",") if eid.strip()]
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

    def return_single_session(field, value):
        for proxmox_schema in proxmox_schemas:
            if value == getattr(proxmox_schema, field, None):
                return [ProxmoxSession(proxmox_schema)]

        raise ProxboxException(
            message=f"No result found for Proxmox Sessions based on the provided {field}",
            detail="Check if the provided parameters are correct",
        )

    try:
        if ip_address is not None:
            return return_single_session("ip_address", ip_address)

        if domain is not None:
            return return_single_session("domain", domain)

        if name is not None:
            return return_single_session("name", name)
    except ProxboxException as error:
        raise error

    try:
        return [ProxmoxSession(px_schema) for px_schema in proxmox_schemas]
    except Exception as error:
        raise ProxboxException(
            message="Could not return Proxmox Sessions", python_exception=f"{error}"
        )


ProxmoxSessionsDep = Annotated[list, Depends(proxmox_sessions)]


def _netbox_field(endpoint: Any, field: str, default: Any = None) -> Any:
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
        password=endpoint.password,
        ssl=endpoint.verify_ssl,
        token=ProxmoxTokenSchema(
            name=endpoint.token_name,
            value=endpoint.token_value,
        ),
    )


def _parse_netbox_endpoint(endpoint: Any) -> ProxmoxSessionSchema:
    ip = None
    ip_address_object = _netbox_field(endpoint, "ip_address")
    if ip_address_object:
        if isinstance(ip_address_object, dict):
            ip_address_with_mask = ip_address_object.get("address")
        else:
            ip_address_with_mask = getattr(ip_address_object, "address", None)
        if ip_address_with_mask:
            ip = ip_address_with_mask.split("/")[0]

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
    )


async def load_proxmox_session_schemas(
    database_session: DatabaseSessionDep,
    source: str = "database",
    endpoint_ids: list[int] | None = None,
) -> list[ProxmoxSessionSchema]:
    """Load configured Proxmox endpoint schemas without creating Proxmox API sessions."""

    if source == "netbox":
        netbox_session = get_netbox_async_session(database_session=database_session)

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
        return [_parse_netbox_endpoint(endpoint) for endpoint in netbox_endpoints]

    query = select(ProxmoxEndpoint)
    if endpoint_ids:
        query = query.where(ProxmoxEndpoint.id.in_(endpoint_ids))
    db_endpoints = database_session.exec(query).all()
    return [_parse_db_endpoint(endpoint) for endpoint in db_endpoints]


async def resolve_proxmox_target_session(
    database_session: DatabaseSessionDep,
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
                return ProxmoxSession(proxmox_schema)
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

    return ProxmoxSession(proxmox_schemas[0])
