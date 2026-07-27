"""FastAPI dependencies and Proxmox endpoint schema loading."""

from __future__ import annotations

import asyncio
import inspect
import threading
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
from proxbox_api.settings_client import (
    get_default_settings,
    get_settings,
    override_settings_for_current_thread,
)
from proxbox_api.types import ProxboxSettingsDict

_NETBOX_ENDPOINT_ID_CHUNK_SIZE = 100
_DB_SETTINGS_REQUEST_TIMEOUT_SECONDS = 0.5
_DB_SETTINGS_INFLIGHT_LOCK = threading.Lock()
_DB_SETTINGS_INFLIGHT: dict[
    asyncio.AbstractEventLoop,
    asyncio.Task[ProxboxSettingsDict],
] = {}


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
    proxmox_endpoint_ids: Annotated[
        str | None,
        Query(
            title="Proxmox Endpoint IDs (plugin alias)",
            description=(
                "Alias for endpoint_ids used by the netbox-proxbox plugin. "
                "Takes precedence over endpoint_ids when both are provided."
            ),
            max_length=255,
        ),
    ] = None,
):
    """
    Default Behavior: Instantiate Proxmox Sessions and return a list of Proxmox Sessions objects.
    If 'name' is provided, return only the Proxmox Session with that name.
    If 'endpoint_ids' or 'proxmox_endpoint_ids' is provided, filter by those database IDs.
    """

    if source not in ("database", "netbox"):
        raise ProxboxException(
            message="Invalid source parameter",
            detail="source must be 'database' or 'netbox'.",
        )

    effective_endpoint_ids = proxmox_endpoint_ids or endpoint_ids

    endpoint_id_list = None
    if effective_endpoint_ids is not None and effective_endpoint_ids.strip():
        if len(effective_endpoint_ids) > 255:
            raise ProxboxException(
                message="Invalid Proxmox endpoint_ids query parameter",
                detail="endpoint_ids exceeds maximum length.",
            )
        try:
            parts = [p.strip() for p in effective_endpoint_ids.split(",") if p.strip()]
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


def _relation_metadata(endpoint: object, prefix: str) -> dict[str, object | None]:
    nested = _netbox_field(endpoint, prefix)
    nested_id = nested_slug = nested_name = None
    if isinstance(nested, dict):
        nested_id = nested.get("id")
        nested_slug = nested.get("slug")
        nested_name = nested.get("name") or nested.get("display")
    elif nested is not None:
        nested_id = getattr(nested, "id", None)
        nested_slug = getattr(nested, "slug", None)
        nested_name = getattr(nested, "name", None) or getattr(nested, "display", None)

    direct_id = _netbox_field(endpoint, f"{prefix}_id", None)
    direct_slug = _netbox_field(endpoint, f"{prefix}_slug", None)
    direct_name = _netbox_field(endpoint, f"{prefix}_name", None)
    return {
        f"{prefix}_id": direct_id if direct_id is not None else nested_id,
        f"{prefix}_slug": direct_slug if direct_slug is not None else nested_slug,
        f"{prefix}_name": direct_name if direct_name is not None else nested_name,
    }


def _parse_db_endpoint(
    endpoint: ProxmoxEndpoint,
    plugin_settings: dict[str, object] | None = None,
) -> ProxmoxSessionSchema:
    settings = plugin_settings or {}
    password = _decrypt_db_secret(
        endpoint=endpoint,
        field="password",
        raw_value=endpoint.password,
        decrypt=endpoint.get_decrypted_password,
    )
    token_value = _decrypt_db_secret(
        endpoint=endpoint,
        field="token_value",
        raw_value=endpoint.token_value,
        decrypt=endpoint.get_decrypted_token_value,
    )
    return ProxmoxSessionSchema(
        name=endpoint.name,
        ip_address=endpoint.ip_address,
        domain=endpoint.domain,
        http_port=endpoint.port,
        user=endpoint.username,
        password=password,
        ssl=endpoint.verify_ssl,
        token=ProxmoxTokenSchema(
            name=endpoint.token_name,
            value=token_value,
        ),
        timeout=(
            endpoint.timeout if endpoint.timeout is not None else settings.get("proxmox_timeout")
        ),
        max_retries=(
            endpoint.max_retries
            if endpoint.max_retries is not None
            else settings.get("proxmox_max_retries")
        ),
        retry_backoff=(
            float(endpoint.retry_backoff)
            if endpoint.retry_backoff is not None
            else settings.get("proxmox_retry_backoff")
        ),
        db_endpoint_id=endpoint.id,
        site_id=endpoint.site_id,
        site_slug=endpoint.site_slug,
        site_name=endpoint.site_name,
        tenant_id=endpoint.tenant_id,
        tenant_slug=endpoint.tenant_slug,
        tenant_name=endpoint.tenant_name,
    )


def proxmox_session_schema_from_endpoint(
    endpoint: ProxmoxEndpoint,
) -> ProxmoxSessionSchema:
    """Build API authority from one caller-owned endpoint snapshot."""

    return _parse_db_endpoint(endpoint)


def _decrypt_db_secret(
    *,
    endpoint: ProxmoxEndpoint,
    field: str,
    raw_value: str | None,
    decrypt: object,
) -> str | None:
    """Decrypt ciphertext without consulting settings for legacy plaintext."""

    if raw_value is None or not raw_value.startswith("enc:"):
        return raw_value

    if not callable(decrypt):  # pragma: no cover - model contract guard
        decrypted_value = None
    else:
        decrypted_value = decrypt()
    if not isinstance(decrypted_value, str) or decrypted_value.startswith("enc:"):
        raise ProxboxException(
            message="Could not decrypt Proxmox endpoint credentials",
            detail=(
                f"Endpoint {endpoint.name!r} has encrypted {field} data, but no usable "
                "encryption key was available within the bounded settings lookup."
            ),
            http_status_code=503,
        )
    return decrypted_value


def _chunk_endpoint_ids(endpoint_ids: list[int]) -> list[list[int]]:
    """Return stable, deduplicated endpoint-ID chunks for NetBox filters."""

    ordered_ids = list(dict.fromkeys(endpoint_ids))
    return [
        ordered_ids[offset : offset + _NETBOX_ENDPOINT_ID_CHUNK_SIZE]
        for offset in range(0, len(ordered_ids), _NETBOX_ENDPOINT_ID_CHUNK_SIZE)
    ]


async def _load_netbox_source_plugin_settings(
    database_session: AsyncSession | Session,
) -> tuple[object, dict[str, object]]:
    """Resolve one NetBox facade and fetch effective plugin settings off-loop."""

    netbox_session = get_netbox_async_session(database_session=database_session)
    if inspect.isawaitable(netbox_session):
        netbox_session = await netbox_session

    plugin_settings = await asyncio.to_thread(
        get_settings,
        netbox_session=netbox_session,
    )
    return netbox_session, dict(plugin_settings)


def _default_db_settings() -> ProxboxSettingsDict:
    return get_default_settings().copy()


async def _fetch_db_transport_settings() -> ProxboxSettingsDict:
    """Fetch transport settings within a small total blocking-I/O budget."""

    defaults = _default_db_settings()
    try:
        async with asyncio.timeout(_DB_SETTINGS_REQUEST_TIMEOUT_SECONDS):
            settings = await asyncio.to_thread(
                get_settings,
                netbox_session=None,
                use_cache=True,
                request_timeout_seconds=_DB_SETTINGS_REQUEST_TIMEOUT_SECONDS,
                cache_fallback=False,
            )
    except Exception as error:  # noqa: BLE001 - endpoint loading must remain available
        logger.warning(
            "Could not load Proxmox transport settings; using deterministic defaults: %s",
            error,
        )
        return defaults
    resolved = defaults.copy()
    resolved.update(settings)
    return resolved


def _clear_db_settings_inflight(
    loop: asyncio.AbstractEventLoop,
    task: asyncio.Future[ProxboxSettingsDict],
) -> None:
    with _DB_SETTINGS_INFLIGHT_LOCK:
        if _DB_SETTINGS_INFLIGHT.get(loop) is task:
            _DB_SETTINGS_INFLIGHT.pop(loop, None)


async def _load_db_transport_settings() -> ProxboxSettingsDict:
    """Single-flight cold database-source settings loads per event loop."""

    loop = asyncio.get_running_loop()
    with _DB_SETTINGS_INFLIGHT_LOCK:
        task = _DB_SETTINGS_INFLIGHT.get(loop)
        if task is None:
            task = loop.create_task(_fetch_db_transport_settings())
            _DB_SETTINGS_INFLIGHT[loop] = task
            task.add_done_callback(
                lambda completed, current_loop=loop: _clear_db_settings_inflight(
                    current_loop,
                    completed,
                )
            )
    return (await asyncio.shield(task)).copy()


async def _load_db_endpoints(
    database_session: AsyncSession | Session,
    query: object,
) -> list[ProxmoxEndpoint]:
    """Execute synchronous SQLModel sessions off the event-loop thread."""

    if isinstance(database_session, Session):
        return await asyncio.to_thread(
            lambda: list(database_session.exec(query).all()),  # type: ignore[call-overload]
        )
    result = database_session.exec(query)  # type: ignore[call-overload]
    if inspect.isawaitable(result):
        result = await result
    return list(result.all())


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
        db_endpoint_id=_netbox_field(endpoint, "id"),
        **_relation_metadata(endpoint, "site"),
        **_relation_metadata(endpoint, "tenant"),
    )


async def load_proxmox_session_schemas(  # noqa: C901
    database_session: AsyncSession | Session,
    source: str = "database",
    endpoint_ids: list[int] | None = None,
) -> list[ProxmoxSessionSchema]:
    """Load configured Proxmox endpoint schemas without creating Proxmox API sessions."""

    if source == "netbox":
        netbox_session, plugin_settings = await _load_netbox_source_plugin_settings(
            database_session
        )

        try:
            url = "/api/plugins/proxbox/endpoints/proxmox/"
            if endpoint_ids:
                selected_ids = set(endpoint_ids)
                netbox_endpoints_by_id: dict[int, object] = {}
                for chunk in _chunk_endpoint_ids(endpoint_ids):
                    endpoints = await rest_list_async(
                        netbox_session,
                        url,
                        query={"id": [str(endpoint_id) for endpoint_id in chunk]},
                    )
                    for endpoint in endpoints:
                        raw_endpoint_id = _netbox_field(endpoint, "id")
                        try:
                            endpoint_id = int(str(raw_endpoint_id))
                        except (TypeError, ValueError):
                            continue
                        if endpoint_id in selected_ids:
                            netbox_endpoints_by_id.setdefault(endpoint_id, endpoint)
                netbox_endpoints = [
                    netbox_endpoints_by_id[endpoint_id]
                    for endpoint_id in dict.fromkeys(endpoint_ids)
                    if endpoint_id in netbox_endpoints_by_id
                ]
            else:
                netbox_endpoints = await rest_list_async(netbox_session, url)
        except JSONDecodeError as error:
            raise ProxboxException(
                message="NetBox returned invalid JSON while fetching Proxmox endpoints",
                python_exception=str(error),
            )
        return [
            _parse_netbox_endpoint(endpoint, plugin_settings)
            for endpoint in netbox_endpoints
            if _netbox_field(endpoint, "enabled", True)
        ]

    query = select(ProxmoxEndpoint).where(ProxmoxEndpoint.enabled == True)  # noqa: E712
    if endpoint_ids:
        query = query.where(ProxmoxEndpoint.id.in_(endpoint_ids))
    db_endpoints = await _load_db_endpoints(database_session, query)
    if not db_endpoints:
        return []

    needs_transport_settings = any(
        endpoint.timeout is None or endpoint.max_retries is None or endpoint.retry_backoff is None
        for endpoint in db_endpoints
    )
    needs_credential_settings = any(
        isinstance(secret, str) and secret.startswith("enc:")
        for endpoint in db_endpoints
        for secret in (endpoint.password, endpoint.token_value)
    )
    needs_settings = needs_transport_settings or needs_credential_settings
    effective_settings: ProxboxSettingsDict | None = (
        await _load_db_transport_settings() if needs_settings else None
    )
    plugin_settings: dict[str, object] = effective_settings or {}

    def parse() -> list[ProxmoxSessionSchema]:
        if effective_settings is not None and needs_credential_settings:
            with override_settings_for_current_thread(effective_settings):
                return [_parse_db_endpoint(endpoint, plugin_settings) for endpoint in db_endpoints]
        else:
            return [_parse_db_endpoint(endpoint, plugin_settings) for endpoint in db_endpoints]

    if isinstance(database_session, Session):
        return await asyncio.to_thread(parse)
    return parse()


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
