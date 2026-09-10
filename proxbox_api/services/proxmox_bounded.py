"""Bounded Proxmox reads compatible with the currently pinned SDK."""

from __future__ import annotations

import json
from typing import Any, cast

from proxmox_sdk.sdk import exceptions as sdk_exceptions
from proxmox_sdk.sdk.exceptions import ResourceException


class ProxmoxResponseTooLargeError(Exception):
    """Raised before a Proxmox response can exceed its byte limit."""


class ProxmoxUnsupportedEncodingError(Exception):
    """Raised when a bounded response is transport-compressed."""


def _legacy_transport(resource: object) -> tuple[Any, str]:
    backend = getattr(resource, "_backend", None)
    path = getattr(resource, "_path", None)
    required = (
        "_ensure_session",
        "_ensure_authenticated",
        "_url_for",
        "_auth",
        "_ssl",
        "_timeout",
        "_proxy",
    )
    if (
        backend is None
        or not isinstance(path, str)
        or not all(hasattr(backend, name) for name in required)
    ):
        raise RuntimeError("The selected Proxmox backend cannot perform bounded HTTPS reads")
    return backend, path


def _legacy_auth(backend: Any, session: Any) -> tuple[dict[str, str], dict[str, str]]:
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        **backend._auth.build_headers("GET"),
    }
    cookies: dict[str, str] = backend._auth.build_cookies()
    if getattr(backend, "_session_external", False) and cookies:
        backend._purge_jar_auth_cookie(session)
        headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
        cookies = {}
    return headers, cookies


async def _read_bounded_json(response: Any, maximum_bytes: int) -> object:
    encoding = response.headers.get("content-encoding", "identity").lower()
    if encoding not in {"", "identity"}:
        raise ProxmoxUnsupportedEncodingError
    if response.content_length is not None and response.content_length > maximum_bytes:
        raise ProxmoxResponseTooLargeError

    body = bytearray()
    async for chunk in response.content.iter_chunked(min(65_536, maximum_bytes + 1)):
        if len(body) + len(chunk) > maximum_bytes:
            raise ProxmoxResponseTooLargeError
        body.extend(chunk)
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"data": body.decode(errors="replace")}


def _unwrap_response(raw: object, response: Any) -> object:
    raw_data = cast(dict[str, object], raw) if isinstance(raw, dict) else None
    if response.status >= 300:
        errors = raw_data.get("errors") if raw_data is not None else None
        content = raw_data.get("data", "") if raw_data is not None else str(raw)
        error_data = cast(dict[str, Any], errors) if isinstance(errors, dict) else None
        raise ResourceException(
            status_code=response.status,
            status_message=response.reason or "",
            content=str(content),
            errors=error_data,
        )
    if raw_data is not None and "data" in raw_data:
        return raw_data["data"]
    return raw


def _legacy_parameters(parameters: dict[str, object]) -> dict[str, object]:
    """Encode values accepted by aiohttp and the Proxmox API."""
    return {
        key: int(value) if isinstance(value, bool) else value
        for key, value in parameters.items()
        if value is not None
    }


async def _legacy_bounded_get(
    resource: object, maximum_bytes: int, parameters: dict[str, object]
) -> object:
    backend, path = _legacy_transport(resource)
    session = await backend._ensure_session()
    await backend._ensure_authenticated(session)
    headers, cookies = _legacy_auth(backend, session)
    options = {
        "method": "GET",
        "url": backend._url_for(path),
        "headers": headers,
        "cookies": cookies,
        "params": _legacy_parameters(parameters),
        "ssl": backend._ssl,
        "timeout": backend._timeout,
        "proxy": backend._proxy,
        "auto_decompress": False,
        "allow_redirects": False,
    }
    async with session.request(**options) as response:
        return _unwrap_response(await _read_bounded_json(response, maximum_bytes), response)


async def bounded_proxmox_get(resource: object, maximum_bytes: int, **parameters: object) -> object:
    """Read JSON without materializing more than ``maximum_bytes`` bytes."""
    get_bounded = getattr(resource, "get_bounded", None)
    if callable(get_bounded):
        try:
            return await get_bounded(maximum_bytes, **parameters)
        except Exception as exc:
            response_too_large = getattr(sdk_exceptions, "ResponseTooLargeError", None)
            if response_too_large is not None and isinstance(exc, response_too_large):
                raise ProxmoxResponseTooLargeError from exc
            raise
    return await _legacy_bounded_get(resource, maximum_bytes, parameters)
