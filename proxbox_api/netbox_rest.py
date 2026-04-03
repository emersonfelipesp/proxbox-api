"""Direct REST helpers for NetBox resources that bypass schema-bound facade traversal."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import random
from collections.abc import AsyncIterable, AsyncIterator
from urllib.parse import urlsplit

from netbox_sdk.client import ApiResponse
from pydantic import BaseModel

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_async_bridge import run_coroutine_blocking
from proxbox_api.netbox_sdk_helpers import to_dict
from proxbox_api.netbox_sdk_sync import SyncProxy
from proxbox_api.utils.retry import _is_transient_netbox_error


def _resolve_netbox_max_concurrent() -> int:
    """Resolve max concurrent NetBox requests from environment."""
    raw = os.environ.get("PROXBOX_NETBOX_MAX_CONCURRENT", "").strip()
    if not raw:
        return 5
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


_netbox_request_semaphore: asyncio.Semaphore | None = None


def _get_netbox_semaphore() -> asyncio.Semaphore:
    """Get or create the global NetBox request semaphore."""
    global _netbox_request_semaphore
    if _netbox_request_semaphore is None:
        _netbox_request_semaphore = asyncio.Semaphore(_resolve_netbox_max_concurrent())
    return _netbox_request_semaphore


def _unwrap_api(nb: object) -> object:
    if isinstance(nb, SyncProxy):
        return object.__getattribute__(nb, "_obj")
    return nb


def _wrap_sync(value: object) -> object:
    if inspect.iscoroutine(value):
        return _wrap_sync(run_coroutine_blocking(value))
    if isinstance(value, list):
        return [_wrap_sync(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_wrap_sync(item) for item in value)
    if isinstance(value, dict):
        return value
    if isinstance(value, (AsyncIterator, AsyncIterable)):
        return _wrap_sync(_collect_async_iter(value))
    if hasattr(value, "serialize") or hasattr(value, "__dict__"):
        return SyncProxy(value)
    return value


def _collect_async_iter(it: object) -> list[object]:
    async def _collect() -> list[object]:
        return [item async for item in it]

    return run_coroutine_blocking(_collect())


def _normalize_path(path: str) -> str:
    normalized = path.strip()
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if not normalized.endswith("/"):
        normalized = f"{normalized}/"
    return normalized


def _detail_path(list_path: str, record_id: object) -> str:
    list_path = _normalize_path(list_path)
    return f"{list_path}{record_id}/"


def _extract_payload(response: ApiResponse) -> object:
    if response.status < 200 or response.status >= 300:
        detail = response.text
        try:
            payload = response.json()
        except json.JSONDecodeError:
            logger.debug(
                "NetBox error response body was not JSON (status=%s)",
                response.status,
                exc_info=True,
            )
            payload = None
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or payload.get("message") or detail)
        raise ProxboxException(
            message="NetBox REST request failed",
            detail=detail,
        )
    return response.json()


def _handle_netbox_error(error: Exception, operation: str) -> None:
    """Log and re-raise NetBox errors with better context."""
    error_str = str(error)
    is_transient = _is_transient_netbox_error(error)

    if is_transient:
        logger.warning(
            "Transient NetBox error during %s: %s",
            operation,
            error_str,
        )
    else:
        logger.error(
            "NetBox error during %s: %s",
            operation,
            error_str,
        )

    if isinstance(error, ProxboxException):
        raise

    raise ProxboxException(
        message=f"NetBox {operation} failed",
        detail=error_str,
        python_exception=error_str,
    ) from error


def _is_duplicate_error(detail: object) -> bool:
    if isinstance(detail, dict):
        return any(_is_duplicate_error(value) for value in detail.values())
    if isinstance(detail, list):
        return any(_is_duplicate_error(value) for value in detail)
    text = str(detail).lower()
    return "already exists" in text or "must be unique" in text


def _candidate_reuse_lookups(
    lookup: dict[str, object],
    payload: dict[str, object],
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    seen: set[tuple[tuple[str, object], ...]] = set()

    def _add(candidate: dict[str, object]) -> None:
        normalized = {key: value for key, value in candidate.items() if value not in (None, "")}
        if not normalized:
            return
        key = tuple(sorted(normalized.items()))
        if key in seen:
            return
        seen.add(key)
        candidates.append(normalized)

    _add(lookup)

    for field in ("slug", "name", "model"):
        value = payload.get(field)
        if value not in (None, ""):
            _add({field: value})

    if payload.get("name") not in (None, "") and payload.get("site") not in (None, ""):
        _add({"name": payload["name"], "site_id": payload["site"]})

    if payload.get("manufacturer") not in (None, "") and payload.get("model") not in (None, ""):
        _add({"manufacturer_id": payload["manufacturer"], "model": payload["model"]})

    return candidates


class RestRecord:
    """Minimal mutable record wrapper for direct NetBox REST resources."""

    def __init__(self, api: object, list_path: str, values: dict[str, object]) -> None:
        object.__setattr__(self, "_api", api)
        object.__setattr__(self, "_list_path", _normalize_path(list_path))
        object.__setattr__(self, "_data", dict(values))
        object.__setattr__(self, "_dirty_fields", set())

    @property
    def id(self) -> object:
        return self._data.get("id")

    @property
    def url(self) -> str | None:
        value = self._data.get("url")
        return value if isinstance(value, str) else None

    @property
    def _detail_path(self) -> str:
        if self.url:
            parsed = urlsplit(self.url)
            if parsed.path:
                return _normalize_path(parsed.path)
        if self.id is None:
            raise ProxboxException(message="Cannot resolve detail path for unsaved NetBox record")
        return _detail_path(self._list_path, self.id)

    def serialize(self) -> dict[str, object]:
        return dict(self._data)

    def dict(self) -> dict[str, object]:
        return self.serialize()

    @property
    def json(self) -> dict[str, object]:
        return self.serialize()

    def get(self, key: str, default: object = None) -> object:
        return self._data.get(key, default)

    def __getattr__(self, name: str) -> object:
        if name in self._data:
            return self._data[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"_api", "_list_path", "_data", "_dirty_fields"}:
            object.__setattr__(self, name, value)
        else:
            self._data[name] = value
            self._dirty_fields.add(name)

    async def save(self) -> RestRecord:
        payload = {
            field: self._data[field]
            for field in object.__getattribute__(self, "_dirty_fields")
            if field in self._data
        }
        if not payload:
            return self
        try:
            response = await self._api.client.request(
                "PATCH",
                self._detail_path,
                payload=payload,
            )
        except Exception as e:
            _handle_netbox_error(e, f"save record {self._detail_path}")
            raise  # Early return via exception

        try:
            response_payload = _extract_payload(response)
        except ProxboxException:
            raise
        except Exception as e:
            _handle_netbox_error(e, f"parse save record {self._detail_path}")
            raise  # Early return via exception

        if not isinstance(response_payload, dict):
            raise ProxboxException(message="NetBox returned invalid JSON for record update")
        object.__setattr__(self, "_data", response_payload)
        object.__setattr__(self, "_dirty_fields", set())
        return self

    async def delete(self) -> bool:
        try:
            response = await self._api.client.request(
                "DELETE", self._detail_path, expect_json=False
            )
        except Exception as e:
            _handle_netbox_error(e, f"delete record {self._detail_path}")
            raise  # Early return via exception

        if response.status not in {200, 204}:
            raise ProxboxException(
                message="NetBox REST request failed",
                detail=response.text,
            )
        return True


async def rest_list_async(
    nb: object, path: str, *, query: dict[str, object] | None = None
) -> list[RestRecord]:
    api = _unwrap_api(nb)
    semaphore = _get_netbox_semaphore()

    async def _do_request() -> list[RestRecord]:
        try:
            response = await api.client.request("GET", _normalize_path(path), query=query)
        except Exception as e:
            _handle_netbox_error(e, f"list {path}")
            raise  # Early return via exception

        try:
            payload = _extract_payload(response)
        except ProxboxException:
            raise
        except Exception as e:
            _handle_netbox_error(e, f"parse list {path}")
            raise  # Early return via exception

        if isinstance(payload, dict):
            results = payload.get("results", [])
        elif isinstance(payload, list):
            results = payload
        else:
            raise ProxboxException(message="NetBox REST list response was not JSON array/object")
        if not isinstance(results, list):
            raise ProxboxException(message="NetBox REST list response missing results list")
        return [
            RestRecord(api, path, item if isinstance(item, dict) else to_dict(item))
            for item in results
        ]

    # Retry loop with semaphore and exponential backoff for transient errors
    max_retries = max(0, int(os.environ.get("PROXBOX_NETBOX_MAX_RETRIES", "3")))
    base_delay = float(os.environ.get("PROXBOX_NETBOX_RETRY_DELAY", "1.0"))

    for attempt in range(max_retries + 1):
        async with semaphore:
            try:
                return await _do_request()
            except Exception as e:
                if attempt == max_retries or not _is_transient_netbox_error(e):
                    raise
                is_conn_refused = (
                    "connection refused" in str(e).lower()
                    or "connect call failed" in str(e).lower()
                )
                exponential_delay = base_delay * (2**attempt)
                if is_conn_refused:
                    exponential_delay = max(exponential_delay, 5.0)
                delay = exponential_delay + random.uniform(0, exponential_delay * 0.5)
                logger.warning(
                    "NetBox request failed (attempt %s/%s), retrying in %ss: %s",
                    attempt + 1,
                    max_retries + 1,
                    delay,
                    str(e)[:200],
                )
        await asyncio.sleep(delay)

    # Should not reach here, but satisfy type checker
    return await _do_request()


def rest_list(nb: object, path: str, *, query: dict[str, object] | None = None) -> list[object]:
    return _wrap_sync(run_coroutine_blocking(rest_list_async(nb, path, query=query)))


async def rest_first_async(
    nb: object,
    path: str,
    *,
    query: dict[str, object] | None = None,
) -> RestRecord | None:
    records = await rest_list_async(nb, path, query=query)
    if not records:
        return None
    return records[0]


async def rest_create_async(nb: object, path: str, payload: dict[str, object]) -> RestRecord:
    api = _unwrap_api(nb)
    semaphore = _get_netbox_semaphore()

    async def _do_request() -> RestRecord:
        try:
            response = await api.client.request("POST", _normalize_path(path), payload=payload)
        except Exception as e:
            _handle_netbox_error(e, f"create {path}")
            raise  # Early return via exception

        try:
            body = _extract_payload(response)
        except ProxboxException:
            raise
        except Exception as e:
            _handle_netbox_error(e, f"parse create {path}")
            raise  # Early return via exception

        if not isinstance(body, dict):
            raise ProxboxException(message="NetBox REST create response was not a JSON object")
        return RestRecord(api, path, body)

    # Retry loop with semaphore and exponential backoff for transient errors
    max_retries = max(0, int(os.environ.get("PROXBOX_NETBOX_MAX_RETRIES", "3")))
    base_delay = float(os.environ.get("PROXBOX_NETBOX_RETRY_DELAY", "1.0"))

    for attempt in range(max_retries + 1):
        async with semaphore:
            try:
                return await _do_request()
            except Exception as e:
                if attempt == max_retries or not _is_transient_netbox_error(e):
                    raise
                is_conn_refused = (
                    "connection refused" in str(e).lower()
                    or "connect call failed" in str(e).lower()
                )
                exponential_delay = base_delay * (2**attempt)
                if is_conn_refused:
                    exponential_delay = max(exponential_delay, 5.0)
                delay = exponential_delay + random.uniform(0, exponential_delay * 0.5)
                logger.warning(
                    "NetBox create failed (attempt %s/%s), retrying in %ss: %s",
                    attempt + 1,
                    max_retries + 1,
                    delay,
                    str(e)[:200],
                )
        await asyncio.sleep(delay)

    # Should not reach here
    return await _do_request()


def rest_create(nb: object, path: str, payload: dict[str, object]) -> object:
    return _wrap_sync(run_coroutine_blocking(rest_create_async(nb, path, payload)))


async def rest_ensure_async(
    nb: object,
    path: str,
    *,
    lookup: dict[str, object],
    payload: dict[str, object],
) -> RestRecord:
    for candidate in _candidate_reuse_lookups(lookup, payload):
        existing = await rest_first_async(
            nb,
            path,
            query={**candidate, "limit": 2},
        )
        if existing:
            return existing
    try:
        return await rest_create_async(nb, path, payload)
    except ProxboxException as error:
        if _is_duplicate_error(error.detail):
            for candidate in _candidate_reuse_lookups(lookup, payload):
                retry = await rest_first_async(
                    nb,
                    path,
                    query={**candidate, "limit": 2},
                )
                if retry:
                    return retry
        raise


async def rest_reconcile_async(  # noqa: C901
    nb: object,
    path: str,
    *,
    lookup: dict[str, object],
    payload: dict[str, object],
    schema: type[BaseModel],
    current_normalizer,
    patchable_fields: set[str] | frozenset[str] | None = None,
) -> RestRecord:
    desired_model = schema.model_validate(payload)
    desired_payload = desired_model.model_dump(exclude_none=True, by_alias=True)

    async def _find_existing() -> RestRecord | None:
        for candidate in _candidate_reuse_lookups(lookup, desired_payload):
            existing_record = await rest_first_async(nb, path, query={**candidate, "limit": 2})
            if existing_record:
                return existing_record
        return None

    async def _scan_existing() -> RestRecord | None:
        """Walk paginated list results — demo NetBox can have >>200 rows so the first page may miss a match."""
        candidates = _candidate_reuse_lookups(lookup, desired_payload)
        page_size = 200
        max_offset = 10_000
        offset = 0
        while offset <= max_offset:
            records = await rest_list_async(
                nb,
                path,
                query={"limit": page_size, "offset": offset},
            )
            if not records:
                return None
            for record in records:
                try:
                    current_model = schema.model_validate(current_normalizer(record.serialize()))
                except Exception:
                    logger.debug(
                        "Skipping NetBox record during reconcile scan (validation failed)",
                        exc_info=True,
                    )
                    continue
                current_payload = current_model.model_dump(exclude_none=True, by_alias=True)
                for candidate in candidates:
                    if all(current_payload.get(key) == value for key, value in candidate.items()):
                        return record
            if len(records) < page_size:
                return None
            offset += page_size
        return None

    async def _reconcile(existing_record: RestRecord) -> RestRecord:
        current_model = schema.model_validate(current_normalizer(existing_record.serialize()))
        current_payload = current_model.model_dump(exclude_none=True, by_alias=True)

        patch_payload = {
            key: value
            for key, value in desired_payload.items()
            if current_payload.get(key) != value
        }
        if patchable_fields is not None:
            allowed = {str(field) for field in patchable_fields}
            patch_payload = {key: value for key, value in patch_payload.items() if key in allowed}
        if patch_payload:
            for field, value in patch_payload.items():
                setattr(existing_record, field, value)
            await existing_record.save()
        return existing_record

    existing = await _find_existing()
    if existing is None:
        try:
            return await rest_create_async(nb, path, desired_payload)
        except ProxboxException:
            # Re-fetch and scan: list filters can miss rows (API quirks); duplicate errors
            # are not always phrased with "already exists" / "must be unique".
            existing = await _find_existing()
            if existing is None:
                existing = await _scan_existing()
            if existing is not None:
                return await _reconcile(existing)
            raise

    return await _reconcile(existing)


async def rest_patch_async(
    nb: object,
    path: str,
    record_id: int,
    payload: dict[str, object],
) -> dict[str, object]:
    """PATCH a single NetBox record by ID with the given fields."""
    api = _unwrap_api(nb)
    semaphore = _get_netbox_semaphore()

    async def _do_request() -> dict[str, object]:
        try:
            response = await api.client.request(
                "PATCH", _detail_path(path, record_id), payload=payload
            )
        except Exception as e:
            _handle_netbox_error(e, f"patch {path}")
            raise  # Early return via exception
        return _extract_payload(response)

    # Retry loop with semaphore and exponential backoff for transient errors
    max_retries = max(0, int(os.environ.get("PROXBOX_NETBOX_MAX_RETRIES", "3")))
    base_delay = float(os.environ.get("PROXBOX_NETBOX_RETRY_DELAY", "1.0"))

    for attempt in range(max_retries + 1):
        async with semaphore:
            try:
                return await _do_request()
            except Exception as e:
                if attempt == max_retries or not _is_transient_netbox_error(e):
                    raise
                is_conn_refused = (
                    "connection refused" in str(e).lower()
                    or "connect call failed" in str(e).lower()
                )
                exponential_delay = base_delay * (2**attempt)
                if is_conn_refused:
                    exponential_delay = max(exponential_delay, 5.0)
                delay = exponential_delay + random.uniform(0, exponential_delay * 0.5)
                logger.warning(
                    "NetBox patch failed (attempt %s/%s), retrying in %ss: %s",
                    attempt + 1,
                    max_retries + 1,
                    delay,
                    str(e)[:200],
                )
        await asyncio.sleep(delay)

    # Should not reach here
    return await _do_request()


def rest_ensure(
    nb: object,
    path: str,
    *,
    lookup: dict[str, object],
    payload: dict[str, object],
) -> object:
    return _wrap_sync(
        run_coroutine_blocking(rest_ensure_async(nb, path, lookup=lookup, payload=payload))
    )


def rest_reconcile(
    nb: object,
    path: str,
    *,
    lookup: dict[str, object],
    payload: dict[str, object],
    schema: type[BaseModel],
    current_normalizer,
    patchable_fields: set[str] | frozenset[str] | None = None,
) -> object:
    return _wrap_sync(
        run_coroutine_blocking(
            rest_reconcile_async(
                nb,
                path,
                lookup=lookup,
                payload=payload,
                schema=schema,
                current_normalizer=current_normalizer,
                patchable_fields=patchable_fields,
            )
        )
    )


def nested_tag_payload(tag: object) -> list[dict[str, object]]:
    slug = getattr(tag, "slug", None)
    name = getattr(tag, "name", None)
    color = getattr(tag, "color", None)
    if isinstance(tag, dict):
        slug = slug or tag.get("slug")
        name = name or tag.get("name")
        color = color or tag.get("color")
    if not slug or not name:
        return []
    payload = {"name": name, "slug": slug}
    if color:
        payload["color"] = color
    return [payload]


async def ensure_tag_async(
    nb: object,
    *,
    name: str,
    slug: str,
    color: str,
    description: str,
) -> RestRecord:
    existing = await rest_first_async(nb, "/api/extras/tags/", query={"slug": slug, "limit": 2})
    if existing:
        return existing
    try:
        return await rest_create_async(
            nb,
            "/api/extras/tags/",
            {
                "name": name,
                "slug": slug,
                "color": color,
                "description": description,
            },
        )
    except ProxboxException:
        retry = await rest_first_async(nb, "/api/extras/tags/", query={"slug": slug, "limit": 2})
        if retry:
            return retry
        raise
