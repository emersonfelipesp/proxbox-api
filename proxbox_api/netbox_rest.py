"""Direct REST helpers for NetBox resources that bypass schema-bound facade traversal."""

from __future__ import annotations

import asyncio
import inspect
import threading
from typing import Any
from urllib.parse import urlsplit

from netbox_sdk.client import ApiResponse

from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_sdk_helpers import to_dict
from proxbox_api.netbox_sdk_sync import SyncProxy


def _run(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {"value": None, "error": None}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except Exception as error:
            result["error"] = error

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if result["error"] is not None:
        raise result["error"]
    return result["value"]


def _unwrap_api(nb: Any) -> Any:
    if isinstance(nb, SyncProxy):
        return object.__getattribute__(nb, "_obj")
    return nb


def _wrap_sync(value: Any) -> Any:
    if inspect.iscoroutine(value):
        return _wrap_sync(_run(value))
    if isinstance(value, list):
        return [_wrap_sync(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_wrap_sync(item) for item in value)
    if isinstance(value, dict):
        return value
    if hasattr(value, "__aiter__"):
        return _wrap_sync(_collect_async_iter(value))
    if hasattr(value, "serialize") or hasattr(value, "__dict__"):
        return SyncProxy(value)
    return value


def _collect_async_iter(it: Any) -> list[Any]:
    async def _collect() -> list[Any]:
        return [item async for item in it]

    return _run(_collect())


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


class RestRecord:
    """Minimal mutable record wrapper for direct NetBox REST resources."""

    def __init__(self, api: Any, list_path: str, values: dict[str, Any]) -> None:
        object.__setattr__(self, "_api", api)
        object.__setattr__(self, "_list_path", _normalize_path(list_path))
        object.__setattr__(self, "_data", dict(values))

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
        if name in {"_api", "_list_path", "_data"}:
            object.__setattr__(self, name, value)
        else:
            self._data[name] = value

    async def save(self) -> RestRecord:
        response = await self._api.client.request(
            "PATCH",
            self._detail_path,
            payload=self.serialize(),
        )
        payload = _extract_payload(response)
        if not isinstance(payload, dict):
            raise ProxboxException(message="NetBox returned invalid JSON for record update")
        object.__setattr__(self, "_data", payload)
        return self

    async def delete(self) -> bool:
        response = await self._api.client.request("DELETE", self._detail_path, expect_json=False)
        if response.status not in {200, 204}:
            raise ProxboxException(
                message="NetBox REST request failed",
                detail=response.text,
            )
        return True


async def rest_list_async(nb: Any, path: str, *, query: dict[str, Any] | None = None) -> list[RestRecord]:
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
        RestRecord(api, path, item if isinstance(item, dict) else to_dict(item))
        for item in results
    ]


def rest_list(nb: Any, path: str, *, query: dict[str, Any] | None = None) -> list[Any]:
    return _wrap_sync(_run(rest_list_async(nb, path, query=query)))


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
    return _wrap_sync(_run(rest_create_async(nb, path, payload)))


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
