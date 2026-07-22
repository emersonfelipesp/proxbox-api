"""NetBox API session creation and dependency wiring."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import Depends
from netbox_sdk.client import NetBoxApiClient
from netbox_sdk.config import Config
from netbox_sdk.facade import Api
from netbox_sdk.schema import build_schema_index
from sqlmodel import Session, select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.constants import NETBOX_SCHEMA_VERSION
from proxbox_api.database import NetBoxEndpoint, get_async_session
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.runtime_settings import get_float
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

if TYPE_CHECKING:
    from typing import Protocol

    class _ConfiguredApi(Protocol):
        config: Config

    class _EndpointResult(Protocol):
        def first(self) -> NetBoxEndpoint | None: ...


_DEFAULT_NETBOX_TIMEOUT = 120.0
_NETBOX_CLIENT_CLOSE_TIMEOUT = 10.0


def _resolve_netbox_timeout() -> float:
    return get_float(
        settings_key="netbox_timeout",
        env="PROXBOX_NETBOX_TIMEOUT",
        default=_DEFAULT_NETBOX_TIMEOUT,
        minimum=1.0,
    )


def netbox_config_from_endpoint(endpoint: NetBoxEndpoint) -> Config:
    """Build netbox-sdk Config from a stored NetBox endpoint (v1 or v2 tokens)."""
    tv = (endpoint.token_version or "v1").strip().lower()
    if tv not in ("v1", "v2"):
        raise ProxboxException(
            message="Invalid token version in stored endpoint",
            detail=f"Token version must be 'v1' or 'v2', got '{tv}'",
        )
    decrypted_key = endpoint.get_decrypted_token_key()
    key = decrypted_key.strip() if decrypted_key else None
    if tv == "v1":
        key = None
    decrypted_token = endpoint.get_decrypted_token()
    return Config(
        base_url=endpoint.url,
        token_version=tv,
        token_key=key,
        token_secret=decrypted_token,
        timeout=_resolve_netbox_timeout(),
        ssl_verify=endpoint.verify_ssl,
    )


def _config_fingerprint(cfg: Config, ssl_verify: bool) -> str:
    fields = {
        "base_url": cfg.base_url or "",
        "ssl_verify": ssl_verify,
        "timeout": cfg.timeout,
        "token_key": cfg.token_key or "",
        "token_secret": cfg.token_secret or "",
        "token_version": cfg.token_version or "",
    }
    serialized = json.dumps(fields, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class _NetBoxApiCacheEntry:
    endpoint_id: int
    fingerprint: str
    api: Api


class _NetBoxApiCacheChanged(RuntimeError):
    """The cache changed while a superseded client was being closed."""


class _NetBoxApiLifecycleClosed(RuntimeError):
    """The application lifecycle is shutting down and rejects new clients."""


def _clear_legacy_api_references(entries: tuple[_NetBoxApiCacheEntry, ...]) -> None:
    """Release process-wide references to retired clients without importing secrets."""
    if not entries:
        return

    retired = tuple(entry.api for entry in entries)
    try:
        from proxbox_api.app import bootstrap
        from proxbox_api.netbox_compat import NetBoxBase

        if any(bootstrap.netbox_session is api for api in retired):
            bootstrap.netbox_session = None
        if any(NetBoxBase.nb is api for api in retired):
            NetBoxBase.nb = None
    except Exception:  # noqa: BLE001
        # Compatibility globals are best-effort and must never block retirement.
        return


class _NetBoxApiLifecycle:
    """Own cached NetBox clients from construction through async retirement."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[int, _NetBoxApiCacheEntry] = {}
        self._pending: dict[int, set[asyncio.Task[None]]] = {}
        self._revision = 0
        self._state = "open"

    def open(self) -> None:
        """Allow acquisitions for a new application lifespan."""
        with self._lock:
            if self._state == "closing":
                raise RuntimeError("NetBox API lifecycle shutdown is still in progress")
            if self._state == "closed":
                self._revision += 1
                self._state = "open"

    def revision(self) -> int:
        """Return the current invalidation revision for DB-read race detection."""
        with self._lock:
            return self._revision

    async def get_or_create(
        self,
        endpoint: NetBoxEndpoint,
        *,
        expected_revision: int | None = None,
    ) -> Api:
        """Return the current client, retiring a superseded fingerprint asynchronously."""
        endpoint_id = endpoint.id
        if endpoint_id is None:
            raise ProxboxException(
                message="NetBox endpoint must be persisted before client acquisition",
                detail="Save the endpoint and refresh its database ID before creating a client",
            )
        cfg = netbox_config_from_endpoint(endpoint)
        fingerprint = _config_fingerprint(cfg, bool(endpoint.verify_ssl))

        with self._lock:
            if self._state != "open":
                raise _NetBoxApiLifecycleClosed("NetBox API lifecycle is closed")

            revision = self._revision if expected_revision is None else expected_revision
            if revision != self._revision:
                raise _NetBoxApiCacheChanged

            cached = self._entries.get(endpoint_id)
            if cached is not None and cached.fingerprint == fingerprint:
                return cached.api

            api = Api(
                client=NetBoxApiClient(cfg),
                schema=build_schema_index(version=NETBOX_SCHEMA_VERSION),
            )
            created = _NetBoxApiCacheEntry(
                endpoint_id=endpoint_id,
                fingerprint=fingerprint,
                api=api,
            )
            self._entries[endpoint_id] = created
            retired = (cached,) if cached is not None else ()
            close_tasks = self._schedule_close_locked(retired)

        _clear_legacy_api_references(retired)
        await self._await_close_tasks(close_tasks)

        if retired:
            with self._lock:
                if self._revision != revision or self._entries.get(endpoint_id) is not created:
                    raise _NetBoxApiCacheChanged

        return api

    async def invalidate(self, endpoint_id: int | None = None) -> int:
        """Detach matching entries atomically and close them after releasing the lock."""
        with self._lock:
            self._revision += 1
            revision = self._revision
            if endpoint_id is None:
                retired = tuple(self._entries.values())
                self._entries.clear()
            else:
                entry = self._entries.pop(endpoint_id, None)
                retired = (entry,) if entry is not None else ()
            self._schedule_close_locked(retired)
            # Repeated/concurrent invalidation joins retirement work already
            # detached by an earlier caller instead of returning while it runs.
            close_tasks = self._pending_tasks_locked(endpoint_id)

        _clear_legacy_api_references(retired)
        await self._await_close_tasks(close_tasks)
        return revision

    async def shutdown(self) -> None:
        """Stop new acquisitions and close every active or already-retiring client."""
        with self._lock:
            self._state = "closing"
            self._revision += 1
            retired = tuple(self._entries.values())
            self._entries.clear()
            self._schedule_close_locked(retired)
            close_tasks = self._pending_tasks_locked(None)

        _clear_legacy_api_references(retired)
        try:
            await self._await_close_tasks(close_tasks)
        finally:
            with self._lock:
                self._state = "closed"

    def _schedule_close_locked(
        self,
        entries: tuple[_NetBoxApiCacheEntry, ...],
    ) -> tuple[asyncio.Task[None], ...]:
        tasks: list[asyncio.Task[None]] = []
        for entry in entries:
            task = asyncio.create_task(self._close_entry(entry))
            self._pending.setdefault(entry.endpoint_id, set()).add(task)
            task.add_done_callback(
                lambda completed, endpoint_id=entry.endpoint_id: self._close_finished(
                    endpoint_id,
                    completed,
                )
            )
            tasks.append(task)
        return tuple(tasks)

    def _pending_tasks_locked(
        self,
        endpoint_id: int | None,
    ) -> tuple[asyncio.Task[None], ...]:
        if endpoint_id is not None:
            return tuple(self._pending.get(endpoint_id, ()))
        return tuple(task for tasks in self._pending.values() for task in tasks)

    def _close_finished(self, endpoint_id: int, task: asyncio.Task[None]) -> None:
        with self._lock:
            pending = self._pending.get(endpoint_id)
            if pending is None:
                return
            pending.discard(task)
            if not pending:
                self._pending.pop(endpoint_id, None)

    async def _await_close_tasks(self, tasks: tuple[asyncio.Task[None], ...]) -> None:
        unique_tasks = tuple(dict.fromkeys(tasks))
        if not unique_tasks:
            return

        waiter = asyncio.gather(*unique_tasks)
        cancelled = False
        while not waiter.done():
            try:
                await asyncio.shield(waiter)
            except asyncio.CancelledError:
                # Finish resource cleanup before propagating caller cancellation.
                cancelled = True

        waiter.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close_entry(self, entry: _NetBoxApiCacheEntry) -> None:
        try:
            close_result = entry.api.client.close()
            if inspect.isawaitable(close_result):
                await asyncio.wait_for(
                    close_result,
                    timeout=_NETBOX_CLIENT_CLOSE_TIMEOUT,
                )
        except TimeoutError:
            logger.warning(
                "Timed out closing retired NetBox API client for endpoint %s after %.1f seconds",
                entry.endpoint_id,
                _NETBOX_CLIENT_CLOSE_TIMEOUT,
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "Failed to close retired NetBox API client for endpoint %s (%s)",
                entry.endpoint_id,
                type(error).__name__,
            )


_API_CACHE = _NetBoxApiLifecycle()


def open_netbox_api_cache() -> None:
    """Open the lifecycle owner for a new FastAPI lifespan or isolated test."""
    _API_CACHE.open()


async def invalidate_netbox_api_cache(endpoint_id: int | None = None) -> int:
    """Close cached clients for one endpoint, or all endpoints when id is ``None``."""
    return await _API_CACHE.invalidate(endpoint_id)


async def close_netbox_api_cache() -> None:
    """Terminally drain every cached or retiring client during app shutdown."""
    await _API_CACHE.shutdown()


async def netbox_api_from_endpoint(
    endpoint: NetBoxEndpoint,
    *,
    expected_revision: int | None = None,
) -> Api:
    """Return the lifecycle-owned netbox-sdk facade for a persisted endpoint."""
    return await _API_CACHE.get_or_create(endpoint, expected_revision=expected_revision)


def _publish_default_netbox_api(api: Api) -> None:
    """Keep compatibility globals aligned with the lifecycle-owned default client."""
    try:
        from proxbox_api.app import bootstrap
        from proxbox_api.netbox_compat import NetBoxBase

        bootstrap.netbox_session = api
        NetBoxBase.nb = api
    except Exception:  # noqa: BLE001
        return


def clear_default_netbox_api() -> None:
    """Clear compatibility globals when the singleton endpoint is unusable."""
    try:
        from proxbox_api.app import bootstrap
        from proxbox_api.netbox_compat import NetBoxBase

        bootstrap.netbox_session = None
        NetBoxBase.nb = None
    except Exception:  # noqa: BLE001
        return


async def refresh_default_netbox_api(
    endpoint: NetBoxEndpoint,
    *,
    expected_revision: int,
) -> bool:
    """Publish a fresh default client unless a newer invalidation won the race."""
    try:
        api = await netbox_api_from_endpoint(
            endpoint,
            expected_revision=expected_revision,
        )
    except (_NetBoxApiCacheChanged, _NetBoxApiLifecycleClosed):
        return False
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "Failed to refresh default NetBox API client for endpoint %s (%s)",
            endpoint.id,
            type(error).__name__,
        )
        return False

    _publish_default_netbox_api(api)
    return True


async def get_netbox_session(
    database_session: Session | AsyncSession = Depends(get_async_session),
    netbox_id: int | None = None,
) -> Api:
    """
    Get NetBox API parameters from database and establish a netbox-sdk API session.

    Args:
        database_session: Async database session dependency. Sync SQLModel sessions
            remain supported for explicit compatibility callers and tests.
        netbox_id: Optional specific NetBox endpoint ID. If not provided, selects by
            ID when multiple endpoints exist, or returns the only endpoint when only
            one exists.

    Returns:
        NetBox API session for the endpoint.

    Raises:
        ProxboxException: If no endpoint found or on error.
    """
    try:
        while True:
            revision = _API_CACHE.revision()
            if netbox_id is not None:
                netbox_endpoint = cast(
                    "NetBoxEndpoint | None",
                    await _maybe_await(
                        database_session.get(
                            NetBoxEndpoint,
                            netbox_id,
                            populate_existing=True,
                        )
                    ),
                )
                if not netbox_endpoint:
                    raise ProxboxException(
                        message=f"NetBox endpoint {netbox_id} not found",
                        detail=f"No endpoint with ID {netbox_id}",
                    )
                if not netbox_endpoint.enabled:
                    raise ProxboxException(
                        message=f"NetBox endpoint {netbox_id} is disabled",
                        detail="Enable the endpoint before requesting a NetBox API session",
                    )
            else:
                result = cast(
                    "_EndpointResult",
                    await _maybe_await(
                        database_session.exec(
                            select(NetBoxEndpoint)
                            .where(NetBoxEndpoint.enabled)
                            .order_by(cast("Any", NetBoxEndpoint.id))
                            .execution_options(populate_existing=True)
                        )
                    ),
                )
                netbox_endpoint = result.first()

            if not netbox_endpoint:
                raise ProxboxException(
                    message="No NetBox endpoint found",
                    detail="Please add a NetBox endpoint in the database",
                )

            try:
                api = await netbox_api_from_endpoint(
                    netbox_endpoint,
                    expected_revision=revision,
                )
            except _NetBoxApiCacheChanged:
                continue

            if netbox_id is None:
                _publish_default_netbox_api(api)
            return api

    except ProxboxException:
        raise

    except Exception as error:
        error_type = type(error).__name__

    # Raise outside the exception handler so the upstream exception (which may
    # contain credentials in its message) is neither chained nor retained.
    raise ProxboxException(
        message="Error establishing NetBox API session",
        python_exception=f"{error_type}: details suppressed",
    )


async def get_netbox_async_session(
    database_session: Session | AsyncSession = Depends(get_async_session),
    netbox_id: int | None = None,
) -> Api:
    """Compatibility alias for the canonical async NetBox dependency provider."""
    return await get_netbox_session(database_session, netbox_id)


def get_netbox_session_sync(database_session: Any, netbox_id: int | None = None) -> Api:
    """Preserve the package's historical synchronous Python API outside event loops."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(get_netbox_session(database_session, netbox_id))
    raise RuntimeError(
        "get_netbox_session_sync() cannot run inside an event loop; "
        "await get_netbox_session() instead"
    )


NetBoxSessionDep = Annotated[Api, Depends(get_netbox_session)]
NetBoxAsyncSessionDep = Annotated[Api, Depends(get_netbox_async_session)]


async def check_netbox_connection(nb: Api) -> dict[str, object]:
    """
    Check NetBox connectivity and return status information.

    Returns:
        dict with keys: available (bool), url (str), error (str or None)
    """
    from proxbox_api.netbox_rest import rest_list_async

    try:
        configured_nb = cast("_ConfiguredApi", nb)
        url = configured_nb.config.base_url
        await rest_list_async(nb, "/api/", query={"limit": 1})
        return {"available": True, "url": url, "error": None}
    except ProxboxException as e:
        return {
            "available": False,
            "url": getattr(nb, "config", None)
            and getattr(configured_nb.config, "base_url", "unknown")
            or "unknown",
            "error": e.detail or e.message,
        }
    except Exception as e:
        return {"available": False, "url": "unknown", "error": str(e)}
