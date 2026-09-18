"""FastAPI dependencies and Proxmox endpoint schema loading."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable
from json import JSONDecodeError
from typing import Annotated

from fastapi import Depends, Query
from proxmox_sdk.sdk.exceptions import ResourceException
from sqlmodel import Session, select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.database import ProxmoxEndpoint, get_async_session
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_list_async
from proxbox_api.schemas.proxmox import ProxmoxSessionSchema, ProxmoxTokenSchema
from proxbox_api.services.interactive_policy import (
    InteractiveDenied,
    acquire_interactive_resource,
    current_interactive_runtime,
)
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


def _upstream_http_status(error: Exception) -> int:
    """Return a validated Proxmox SDK HTTP status or a generic gateway failure."""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, ResourceException):
            status_code = current.status_code
            return status_code if 400 <= status_code <= 599 else 502
        current = current.__cause__ or current.__context__
    return 502


def _session_acquisition_error(error: Exception) -> ProxboxException:
    """Translate session construction failures into a safe operator contract."""
    status_code = _upstream_http_status(error)
    detail: dict[str, object] = {
        "reason": "proxmox_session_acquisition_failed",
        "error_type": type(error).__name__,
    }
    if status_code != 502:
        detail["upstream_status"] = status_code
    return ProxboxException(
        message="Could not return Proxmox Sessions",
        detail=detail,
        python_exception=str(error),
        public_python_exception=type(error).__name__,
        http_status_code=status_code,
        redact_log_details=True,
    )


_DB_SETTINGS_INFLIGHT_LOCK = threading.Lock()
_DB_SETTINGS_INFLIGHT: dict[
    asyncio.AbstractEventLoop,
    asyncio.Task[ProxboxSettingsDict],
] = {}


def _parse_endpoint_ids(raw_endpoint_ids: str | None) -> list[int] | None:
    if raw_endpoint_ids is None or not raw_endpoint_ids.strip():
        return None
    if len(raw_endpoint_ids) > 255:
        raise ProxboxException(
            message="Invalid Proxmox endpoint_ids query parameter",
            detail="endpoint_ids exceeds maximum length.",
        )

    parts = [part.strip() for part in raw_endpoint_ids.split(",") if part.strip()]
    if len(parts) > 100:
        raise ProxboxException(
            message="Invalid Proxmox endpoint_ids query parameter",
            detail="Too many endpoint IDs specified.",
        )
    try:
        return [int(endpoint_id) for endpoint_id in parts]
    except ValueError as error:
        raise ProxboxException(
            message="Invalid Proxmox endpoint_ids query parameter",
            detail="endpoint_ids must be a comma-separated list of integers.",
            python_exception=str(error),
        ) from error


async def _create_filtered_session(
    proxmox_schemas: list[ProxmoxSessionSchema],
    field: str,
    value: str,
) -> list[ProxmoxSession]:
    schema = next(
        (item for item in proxmox_schemas if value == getattr(item, field, None)),
        None,
    )
    if schema is None:
        raise ProxboxException(
            message=f"No result found for Proxmox Sessions based on the provided {field}",
            detail="Check if the provided parameters are correct",
        )
    try:
        return [await _create_request_session(schema)]
    except InteractiveDenied:
        raise
    except Exception as error:
        raise _session_acquisition_error(error) from error


async def _create_all_sessions(
    proxmox_schemas: list[ProxmoxSessionSchema],
) -> list[ProxmoxSession]:
    results = await asyncio.gather(
        *[_create_request_session(schema) for schema in proxmox_schemas],
        return_exceptions=True,
    )
    failures = [result for result in results if isinstance(result, BaseException)]
    sessions = [result for result in results if not isinstance(result, BaseException)]
    if not failures:
        return sessions

    failure = failures[0]
    if current_interactive_runtime() is None:
        await _close_sessions_after_failed_acquisition(sessions)
    if not isinstance(failure, Exception):
        raise failure
    raise _session_acquisition_error(failure) from failure


async def _close_failed_acquisition_session(session: ProxmoxSession) -> None:
    close_method = getattr(session, "aclose", None)
    if not callable(close_method):
        return
    try:
        await close_method()
    except BaseException as error:
        logger.debug(
            "Failed to clean up partially acquired proxmox session: %s",
            type(error).__name__,
        )


async def _close_sessions_after_failed_acquisition(
    sessions: list[ProxmoxSession],
) -> None:
    cleanup = asyncio.gather(*[_close_failed_acquisition_session(session) for session in sessions])
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await cleanup
        raise


async def proxmox_sessions(
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

    if source not in {"database", "netbox"}:
        raise ProxboxException(
            message="Invalid source parameter",
            detail="source must be 'database' or 'netbox'.",
        )

    endpoint_id_list = _parse_endpoint_ids(proxmox_endpoint_ids or endpoint_ids)

    proxmox_schemas = await _load_request_schemas(
        database_session=database_session,
        source=source,
        endpoint_ids=endpoint_id_list,
    )

    if ip_address is not None:
        return await _create_filtered_session(proxmox_schemas, "ip_address", ip_address)

    if domain is not None:
        return await _create_filtered_session(proxmox_schemas, "domain", domain)

    if name is not None:
        return await _create_filtered_session(proxmox_schemas, "name", name)

    return await _create_all_sessions(proxmox_schemas)


async def proxmox_sessions_dep(
    sessions: Annotated[list[ProxmoxSession], Depends(proxmox_sessions)],
):
    owner = current_interactive_runtime()
    try:
        yield sessions
    finally:
        if owner is None:
            await close_proxmox_sessions(sessions)


async def _load_request_schemas(
    *, database_session: AsyncSession | Session, source: str, endpoint_ids: list[int] | None
) -> list[ProxmoxSessionSchema]:
    async def load() -> list[ProxmoxSessionSchema]:
        return await load_proxmox_session_schemas(
            database_session=database_session, source=source, endpoint_ids=endpoint_ids
        )

    if current_interactive_runtime() is None:
        return await load()

    async def discard(schemas: list[ProxmoxSessionSchema]) -> None:
        """Release late schema references without connecting or delivering them."""

    return await _interactive_acquisition(load, discard)


async def _create_request_session(schema: ProxmoxSessionSchema) -> ProxmoxSession:
    if current_interactive_runtime() is None:
        return await ProxmoxSession.create(schema)
    return await _interactive_acquisition(
        lambda: ProxmoxSession.create(schema), _close_interactive_session
    )


async def _interactive_acquisition[T](
    acquisition: Callable[[], Awaitable[T]], close: Callable[[T], Awaitable[object]]
) -> T:
    try:
        return await acquire_interactive_resource(acquisition, close)
    except InteractiveDenied:
        raise
    except Exception:
        raise ProxboxException(
            message="Interactive Proxmox provider is unavailable.",
            http_status_code=502,
            redact_log_details=True,
        ) from None


async def _close_interactive_session(session: ProxmoxSession) -> None:
    """Let the interactive owner retain cleanup failure as uncertainty."""
    await session.aclose()


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
    current_interactive_runtime()
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

    current_interactive_runtime()
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
    current_interactive_runtime()
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


def _netbox_endpoint_id(endpoint: object) -> int | None:
    try:
        return int(str(_netbox_field(endpoint, "id")))
    except (TypeError, ValueError):
        return None


async def _load_selected_netbox_endpoints(
    netbox_session: object,
    url: str,
    endpoint_ids: list[int],
) -> list[object]:
    selected_ids = set(endpoint_ids)
    endpoints_by_id: dict[int, object] = {}
    for chunk in _chunk_endpoint_ids(endpoint_ids):
        current_interactive_runtime()
        endpoints = await rest_list_async(
            netbox_session,
            url,
            query={"id": [str(endpoint_id) for endpoint_id in chunk]},
        )
        for endpoint in endpoints:
            endpoint_id = _netbox_endpoint_id(endpoint)
            if endpoint_id in selected_ids:
                endpoints_by_id.setdefault(endpoint_id, endpoint)
    return [
        endpoints_by_id[endpoint_id]
        for endpoint_id in dict.fromkeys(endpoint_ids)
        if endpoint_id in endpoints_by_id
    ]


async def _load_netbox_endpoints(
    netbox_session: object,
    endpoint_ids: list[int] | None,
) -> list[object]:
    url = "/api/plugins/proxbox/endpoints/proxmox/"
    try:
        if endpoint_ids:
            return await _load_selected_netbox_endpoints(netbox_session, url, endpoint_ids)
        return await rest_list_async(netbox_session, url)
    except JSONDecodeError as error:
        raise ProxboxException(
            message="NetBox returned invalid JSON while fetching Proxmox endpoints",
            python_exception=str(error),
        ) from error


async def _load_netbox_schemas(
    database_session: AsyncSession | Session,
    endpoint_ids: list[int] | None,
) -> list[ProxmoxSessionSchema]:
    netbox_session, plugin_settings = await _load_netbox_source_plugin_settings(database_session)
    current_interactive_runtime()
    endpoints = await _load_netbox_endpoints(netbox_session, endpoint_ids)
    return [
        _parse_netbox_endpoint(endpoint, plugin_settings)
        for endpoint in endpoints
        if _netbox_field(endpoint, "enabled", True)
    ]


def _database_endpoint_query(endpoint_ids: list[int] | None) -> object:
    query = select(ProxmoxEndpoint).where(ProxmoxEndpoint.enabled == True)  # noqa: E712
    if endpoint_ids:
        query = query.where(ProxmoxEndpoint.id.in_(endpoint_ids))
    return query


def _database_settings_requirements(
    endpoints: list[ProxmoxEndpoint],
) -> tuple[bool, bool]:
    needs_transport = any(
        endpoint.timeout is None or endpoint.max_retries is None or endpoint.retry_backoff is None
        for endpoint in endpoints
    )
    needs_credentials = any(
        isinstance(secret, str) and secret.startswith("enc:")
        for endpoint in endpoints
        for secret in (endpoint.password, endpoint.token_value)
    )
    return needs_transport, needs_credentials


def _parse_database_endpoints(
    endpoints: list[ProxmoxEndpoint],
    settings: ProxboxSettingsDict | None,
    needs_credentials: bool,
) -> list[ProxmoxSessionSchema]:
    plugin_settings: dict[str, object] = settings or {}
    if settings is not None and needs_credentials:
        with override_settings_for_current_thread(settings):
            return [_parse_db_endpoint(endpoint, plugin_settings) for endpoint in endpoints]
    return [_parse_db_endpoint(endpoint, plugin_settings) for endpoint in endpoints]


async def _load_database_schemas(
    database_session: AsyncSession | Session,
    endpoint_ids: list[int] | None,
) -> list[ProxmoxSessionSchema]:
    query = _database_endpoint_query(endpoint_ids)
    db_endpoints = await _load_db_endpoints(database_session, query)
    if not db_endpoints:
        return []

    needs_transport_settings, needs_credential_settings = _database_settings_requirements(
        db_endpoints
    )
    needs_settings = needs_transport_settings or needs_credential_settings
    effective_settings: ProxboxSettingsDict | None = (
        await _load_db_transport_settings() if needs_settings else None
    )
    if isinstance(database_session, Session):
        return await asyncio.to_thread(
            _parse_database_endpoints,
            db_endpoints,
            effective_settings,
            needs_credential_settings,
        )
    return _parse_database_endpoints(
        db_endpoints,
        effective_settings,
        needs_credential_settings,
    )


async def load_proxmox_session_schemas(
    database_session: AsyncSession | Session,
    source: str = "database",
    endpoint_ids: list[int] | None = None,
) -> list[ProxmoxSessionSchema]:
    """Load configured Proxmox endpoint schemas without creating Proxmox API sessions."""

    if source == "netbox":
        return await _load_netbox_schemas(database_session, endpoint_ids)
    return await _load_database_schemas(database_session, endpoint_ids)


async def resolve_proxmox_target_session(
    database_session: AsyncSession | Session,
    *,
    source: str = "database",
    endpoint_id: int | None = None,
    name: str | None = None,
    domain: str | None = None,
    ip_address: str | None = None,
) -> ProxmoxSession:
    """Resolve a single Proxmox target for generated live proxy routes."""

    proxmox_schemas = await load_proxmox_session_schemas(
        database_session=database_session,
        source=source,
        endpoint_ids=[endpoint_id] if endpoint_id is not None else None,
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
