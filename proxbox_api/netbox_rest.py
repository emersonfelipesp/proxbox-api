"""Direct REST helpers for NetBox resources that bypass schema-bound facade traversal."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any
from urllib.parse import urlsplit

from netbox_sdk.client import ApiResponse
from pydantic import BaseModel

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_async_bridge import run_coroutine_blocking
from proxbox_api.netbox_sdk_helpers import to_dict
from proxbox_api.netbox_sdk_sync import SyncProxy


def _unwrap_api(nb: Any) -> Any:
    if isinstance(nb, SyncProxy):
        return object.__getattribute__(nb, "_obj")
    return nb


def _wrap_sync(value: Any) -> Any:
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


def _collect_async_iter(it: Any) -> list[Any]:
    async def _collect() -> list[Any]:
        return [item async for item in it]

    return run_coroutine_blocking(_collect())


def _normalize_path(path: str) -> str:
    normalized = path.strip()
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if not normalized.endswith("/"):
        normalized = f"{normalized}/"
    return normalized


def _detail_path(list_path: str, record_id: Any) -> str:
    list_path = _normalize_path(list_path)
    return f"{list_path}{record_id}/"


def _extract_payload(response: ApiResponse) -> Any:
    if response.status < 200 or response.status >= 300:
        detail = response.text
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or payload.get("message") or detail)
        raise ProxboxException(
            message="NetBox REST request failed",
            detail=detail,
        )
    return response.json()


def _is_duplicate_error(detail: Any) -> bool:
    if isinstance(detail, dict):
        return any(_is_duplicate_error(value) for value in detail.values())
    if isinstance(detail, list):
        return any(_is_duplicate_error(value) for value in detail)
    text = str(detail).lower()
    return "already exists" in text or "must be unique" in text


def _candidate_reuse_lookups(
    lookup: dict[str, Any],
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, Any], ...]] = set()

    def _add(candidate: dict[str, Any]) -> None:
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

    def __init__(self, api: Any, list_path: str, values: dict[str, Any]) -> None:
        object.__setattr__(self, "_api", api)
        object.__setattr__(self, "_list_path", _normalize_path(list_path))
        object.__setattr__(self, "_data", dict(values))
        object.__setattr__(self, "_dirty_fields", set())

    @property
    def id(self) -> Any:
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

    def serialize(self) -> dict[str, Any]:
        return dict(self._data)

    def dict(self) -> dict[str, Any]:
        return self.serialize()

    @property
    def json(self) -> dict[str, Any]:
        return self.serialize()

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __getattr__(self, name: str) -> Any:
        if name in self._data:
            return self._data[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
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
        response = await self._api.client.request(
            "PATCH",
            self._detail_path,
            payload=payload,
        )
        payload = _extract_payload(response)
        if not isinstance(payload, dict):
            raise ProxboxException(message="NetBox returned invalid JSON for record update")
        object.__setattr__(self, "_data", payload)
        object.__setattr__(self, "_dirty_fields", set())
        return self

    async def delete(self) -> bool:
        response = await self._api.client.request("DELETE", self._detail_path, expect_json=False)
        if response.status not in {200, 204}:
            raise ProxboxException(
                message="NetBox REST request failed",
                detail=response.text,
            )
        return True


async def rest_list_async(
    nb: Any, path: str, *, query: dict[str, Any] | None = None
) -> list[RestRecord]:
    api = _unwrap_api(nb)
    response = await api.client.request("GET", _normalize_path(path), query=query)
    payload = _extract_payload(response)
    if isinstance(payload, dict):
        results = payload.get("results", [])
    elif isinstance(payload, list):
        results = payload
    else:
        raise ProxboxException(message="NetBox REST list response was not JSON array/object")
    if not isinstance(results, list):
        raise ProxboxException(message="NetBox REST list response missing results list")
    return [
        RestRecord(api, path, item if isinstance(item, dict) else to_dict(item)) for item in results
    ]


def rest_list(nb: Any, path: str, *, query: dict[str, Any] | None = None) -> list[Any]:
    return _wrap_sync(run_coroutine_blocking(rest_list_async(nb, path, query=query)))


async def rest_first_async(
    nb: Any,
    path: str,
    *,
    query: dict[str, Any] | None = None,
) -> RestRecord | None:
    records = await rest_list_async(nb, path, query=query)
    if not records:
        return None
    return records[0]


async def rest_create_async(nb: Any, path: str, payload: dict[str, Any]) -> RestRecord:
    api = _unwrap_api(nb)
    response = await api.client.request("POST", _normalize_path(path), payload=payload)
    body = _extract_payload(response)
    if not isinstance(body, dict):
        raise ProxboxException(message="NetBox REST create response was not a JSON object")
    return RestRecord(api, path, body)


def rest_create(nb: Any, path: str, payload: dict[str, Any]) -> Any:
    return _wrap_sync(run_coroutine_blocking(rest_create_async(nb, path, payload)))


async def rest_ensure_async(
    nb: Any,
    path: str,
    *,
    lookup: dict[str, Any],
    payload: dict[str, Any],
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


async def rest_reconcile_async(
    nb: Any,
    path: str,
    *,
    lookup: dict[str, Any],
    payload: dict[str, Any],
    schema: type[BaseModel],
    current_normalizer,
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
        records = await rest_list_async(nb, path, query={"limit": 200})
        candidates = _candidate_reuse_lookups(lookup, desired_payload)
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
        return None

    async def _reconcile(existing_record: RestRecord) -> RestRecord:
        current_model = schema.model_validate(current_normalizer(existing_record.serialize()))
        current_payload = current_model.model_dump(exclude_none=True, by_alias=True)

        patch_payload = {
            key: value
            for key, value in desired_payload.items()
            if current_payload.get(key) != value
        }
        if patch_payload:
            for field, value in patch_payload.items():
                setattr(existing_record, field, value)
            await existing_record.save()
        return existing_record

    existing = await _find_existing()
    if existing is None:
        try:
            return await rest_create_async(nb, path, desired_payload)
        except ProxboxException as error:
            existing = await _find_existing()
            if existing is None and _is_duplicate_error(error.detail):
                existing = await _scan_existing()
            if existing is not None:
                return await _reconcile(existing)
            raise

    return await _reconcile(existing)


def rest_ensure(
    nb: Any,
    path: str,
    *,
    lookup: dict[str, Any],
    payload: dict[str, Any],
) -> Any:
    return _wrap_sync(
        run_coroutine_blocking(rest_ensure_async(nb, path, lookup=lookup, payload=payload))
    )


def rest_reconcile(
    nb: Any,
    path: str,
    *,
    lookup: dict[str, Any],
    payload: dict[str, Any],
    schema: type[BaseModel],
    current_normalizer,
) -> Any:
    return _wrap_sync(
        run_coroutine_blocking(
            rest_reconcile_async(
                nb,
                path,
                lookup=lookup,
                payload=payload,
                schema=schema,
                current_normalizer=current_normalizer,
            )
        )
    )


def nested_tag_payload(tag: Any) -> list[dict[str, Any]]:
    slug = getattr(tag, "slug", None) or getattr(tag, "get", lambda *args, **kwargs: None)("slug")
    name = getattr(tag, "name", None) or getattr(tag, "get", lambda *args, **kwargs: None)("name")
    if not slug or not name:
        return []
    payload = {"name": name, "slug": slug}
    color = getattr(tag, "color", None) or getattr(tag, "get", lambda *args, **kwargs: None)(
        "color"
    )
    if color:
        payload["color"] = color
    return [payload]


async def ensure_tag_async(
    nb: Any,
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
