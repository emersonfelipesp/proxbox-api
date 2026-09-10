from __future__ import annotations

from typing import Any

import aiohttp
import pytest
from aiohttp import web
from proxmox_sdk.sdk.exceptions import ResourceException

from proxbox_api.services.proxmox_bounded import (
    ProxmoxResponseTooLargeError,
    ProxmoxUnsupportedEncodingError,
    bounded_proxmox_get,
)


class _Content:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.requested_chunk_size: int | None = None

    async def iter_chunked(self, size: int):
        self.requested_chunk_size = size
        for chunk in self.chunks:
            yield chunk


class _Response:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        status: int = 200,
        reason: str = "OK",
        headers: dict[str, str] | None = None,
        content_length: int | None = None,
    ) -> None:
        self.content = _Content(chunks)
        self.status = status
        self.reason = reason
        self.headers = headers or {}
        self.content_length = content_length


class _RequestContext:
    def __init__(self, response: _Response) -> None:
        self.response = response

    async def __aenter__(self) -> _Response:
        return self.response

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.options: dict[str, object] = {}

    def request(self, **options: object) -> _RequestContext:
        self.options = options
        return _RequestContext(self.response)


class _Auth:
    def build_headers(self, method: str) -> dict[str, str]:
        assert method == "GET"
        return {"Authorization": "PVEAPIToken=redacted"}

    def build_cookies(self) -> dict[str, str]:
        return {}


class _TicketAuth:
    def build_headers(self, method: str) -> dict[str, str]:
        assert method == "GET"
        return {}

    def build_cookies(self) -> dict[str, str]:
        return {"PVEAuthCookie": "redacted-ticket"}


class _Backend:
    def __init__(self, response: _Response) -> None:
        self._auth = _Auth()
        self._ssl = True
        self._timeout = object()
        self._proxy = None
        self._session_external = False
        self.session = _Session(response)
        self.authenticated = False

    async def _ensure_session(self) -> _Session:
        return self.session

    async def _ensure_authenticated(self, session: _Session) -> None:
        assert session is self.session
        self.authenticated = True

    def _url_for(self, path: str) -> str:
        return f"https://pve.example.test:8006{path}"


class _LegacyResource:
    def __init__(self, response: _Response) -> None:
        self._backend = _Backend(response)
        self._path = "/api2/json/cluster/metrics/export"


async def _network_resource(
    handler: Any, auth: object
) -> tuple[_LegacyResource, web.AppRunner, aiohttp.ClientSession]:
    application = web.Application()
    application.router.add_get("/api2/json/cluster/metrics/export", handler)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets if site._server is not None else []
    assert sockets
    port = sockets[0].getsockname()[1]

    resource = _LegacyResource(_Response([]))
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    resource._backend._auth = auth
    resource._backend._timeout = aiohttp.ClientTimeout(total=2)
    resource._backend.session = session  # type: ignore[assignment]
    resource._backend._url_for = lambda path: f"http://127.0.0.1:{port}{path}"  # type: ignore[method-assign]
    return resource, runner, session


async def test_legacy_bounded_get_streams_authenticated_identity_response() -> None:
    resource = _LegacyResource(_Response([b'{"data":[{"id":"node/pve"}]}']))

    result = await bounded_proxmox_get(resource, 128, history=True, ignored=None)

    assert result == [{"id": "node/pve"}]
    assert resource._backend.authenticated is True
    assert resource._backend.session.options == {
        "method": "GET",
        "url": "https://pve.example.test:8006/api2/json/cluster/metrics/export",
        "headers": {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": "PVEAPIToken=redacted",
        },
        "cookies": {},
        "params": {"history": 1},
        "ssl": True,
        "timeout": resource._backend._timeout,
        "proxy": None,
        "auto_decompress": False,
        "allow_redirects": False,
    }


async def test_legacy_bounded_get_encodes_disabled_boolean() -> None:
    resource = _LegacyResource(_Response([b'{"data":[]}']))

    await bounded_proxmox_get(resource, 128, history=False, **{"local-only": False})

    assert resource._backend.session.options["params"] == {
        "history": 0,
        "local-only": 0,
    }


@pytest.mark.parametrize("enabled", [False, True])
async def test_legacy_boolean_parameters_serialize_over_real_http(enabled: bool) -> None:
    captured: dict[str, str] = {}

    async def handler(request: web.Request) -> web.Response:
        captured.update(request.query)
        return web.json_response({"data": []})

    resource, runner, session = await _network_resource(handler, _Auth())
    try:
        assert (
            await bounded_proxmox_get(resource, 128, history=enabled, **{"local-only": enabled})
            == []
        )
    finally:
        await session.close()
        await runner.cleanup()

    expected = "1" if enabled else "0"
    assert captured == {"history": expected, "local-only": expected}


async def test_legacy_bounded_get_rejects_redirect_without_following() -> None:
    response = _Response([b'{"data":"redirect rejected"}'], status=302, reason="Found")
    resource = _LegacyResource(response)

    with pytest.raises(ResourceException) as exc_info:
        await bounded_proxmox_get(resource, 128)

    assert exc_info.value.status_code == 302
    assert resource._backend.session.options["allow_redirects"] is False


@pytest.mark.parametrize("auth", [_Auth(), _TicketAuth()])
async def test_legacy_authenticated_request_does_not_follow_redirect(auth: object) -> None:
    requests = 0

    async def redirected(_request: web.Request) -> web.Response:
        nonlocal requests
        requests += 1
        return web.json_response({"data": []})

    async def handler(request: web.Request) -> web.Response:
        nonlocal requests
        requests += 1
        raise web.HTTPFound(location=f"{request.scheme}://{request.host}/redirected")

    application = web.Application()
    application.router.add_get("/api2/json/cluster/metrics/export", handler)
    application.router.add_get("/redirected", redirected)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets if site._server is not None else []
    assert sockets
    port = sockets[0].getsockname()[1]
    resource = _LegacyResource(_Response([]))
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    resource._backend._auth = auth
    resource._backend._timeout = aiohttp.ClientTimeout(total=2)
    resource._backend.session = session  # type: ignore[assignment]
    resource._backend._url_for = lambda path: f"http://127.0.0.1:{port}{path}"  # type: ignore[method-assign]
    try:
        with pytest.raises(ResourceException) as exc_info:
            await bounded_proxmox_get(resource, 128)
    finally:
        await session.close()
        await runner.cleanup()

    assert exc_info.value.status_code == 302
    assert requests == 1


@pytest.mark.parametrize(
    "response",
    [
        _Response([b"12345", b"6"], content_length=None),
        _Response([], content_length=6),
    ],
)
async def test_legacy_bounded_get_rejects_response_over_limit(response: _Response) -> None:
    with pytest.raises(ProxmoxResponseTooLargeError):
        await bounded_proxmox_get(_LegacyResource(response), 5)


async def test_legacy_bounded_get_rejects_transport_compression() -> None:
    response = _Response([b"{}"], headers={"content-encoding": "gzip"})

    with pytest.raises(ProxmoxUnsupportedEncodingError):
        await bounded_proxmox_get(_LegacyResource(response), 10)


async def test_legacy_bounded_get_preserves_bounded_provider_error() -> None:
    response = _Response(
        [b'{"data":"denied","errors":{"permission":"missing"}}'],
        status=403,
        reason="Forbidden",
    )

    with pytest.raises(ResourceException) as exc_info:
        await bounded_proxmox_get(_LegacyResource(response), 128)

    assert exc_info.value.status_code == 403
    assert exc_info.value.content == "denied"
    assert exc_info.value.errors == {"permission": "missing"}


async def test_public_bounded_method_is_preferred() -> None:
    class PublicResource:
        async def get_bounded(self, maximum_bytes: int, **parameters: Any) -> object:
            return {"maximum_bytes": maximum_bytes, "parameters": parameters}

    result = await bounded_proxmox_get(PublicResource(), 42, history=True)

    assert result == {"maximum_bytes": 42, "parameters": {"history": True}}
