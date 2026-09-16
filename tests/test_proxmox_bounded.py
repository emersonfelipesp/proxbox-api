from __future__ import annotations

from typing import Any

import pytest
from proxmox_sdk.sdk.exceptions import (
    ProxmoxRedirectError,
    ResourceException,
    ResponseTooLargeError,
    UnsupportedResponseEncodingError,
)

from proxbox_api.services.proxmox_bounded import (
    ProxmoxResponseTooLargeError,
    ProxmoxUnsupportedEncodingError,
    bounded_proxmox_get,
)


class _RecordingResource:
    def __init__(
        self,
        *,
        result: object = None,
        error: BaseException | None = None,
    ) -> None:
        self.maximum_bytes: int | None = None
        self.kwargs: dict[str, object] | None = None
        self._result = result
        self._error = error

    async def get_bounded(self, maximum_bytes: int, **parameters: Any) -> object:
        self.maximum_bytes = maximum_bytes
        self.kwargs = parameters
        if self._error is not None:
            raise self._error
        return self._result


async def test_bounded_get_encodes_booleans_and_drops_none() -> None:
    resource = _RecordingResource(result=[])

    result = await bounded_proxmox_get(
        resource,
        128,
        history=True,
        local_only=False,
        start_time=None,
        **{"local-only": False},
    )

    assert result == []
    assert resource.kwargs == {"history": 1, "local_only": 0, "local-only": 0}


async def test_bounded_get_forwards_maximum_bytes() -> None:
    resource = _RecordingResource(result={"ok": True})

    await bounded_proxmox_get(resource, 4096)

    assert resource.maximum_bytes == 4096


async def test_bounded_get_maps_response_too_large() -> None:
    cause = ResponseTooLargeError(128)
    resource = _RecordingResource(error=cause)

    with pytest.raises(ProxmoxResponseTooLargeError) as exc_info:
        await bounded_proxmox_get(resource, 128)

    assert exc_info.value.__cause__ is cause


async def test_bounded_get_maps_unsupported_encoding() -> None:
    cause = UnsupportedResponseEncodingError()
    resource = _RecordingResource(error=cause)

    with pytest.raises(ProxmoxUnsupportedEncodingError) as exc_info:
        await bounded_proxmox_get(resource, 128)

    assert exc_info.value.__cause__ is cause


async def test_bounded_get_propagates_resource_exception() -> None:
    error = ResourceException(
        status_code=403,
        status_message="Forbidden",
        content="denied",
        errors={"permission": "missing"},
    )
    resource = _RecordingResource(error=error)

    with pytest.raises(ResourceException) as exc_info:
        await bounded_proxmox_get(resource, 128)

    assert exc_info.value.status_code == 403
    assert exc_info.value.content == "denied"
    assert exc_info.value.errors == {"permission": "missing"}


async def test_bounded_get_propagates_redirect_error() -> None:
    error = ProxmoxRedirectError(302, "https://other.example/loc")
    resource = _RecordingResource(error=error)

    with pytest.raises(ProxmoxRedirectError) as exc_info:
        await bounded_proxmox_get(resource, 128)

    assert exc_info.value is error


@pytest.mark.parametrize("enabled", [False, True])
async def test_sdk_bounded_get_encodes_booleans_over_real_http(enabled: bool) -> None:
    """The real SDK transport receives ``0``/``1``: yarl rejects Python booleans."""
    from aiohttp import web
    from proxmox_sdk.sdk.auth.token import TokenAuth
    from proxmox_sdk.sdk.backends.https import HttpsBackend
    from proxmox_sdk.sdk.resource import ProxmoxResource
    from proxmox_sdk.sdk.services import SERVICES

    captured: dict[str, str] = {}

    async def handler(request: web.Request) -> web.Response:
        captured.update(request.query)
        return web.json_response({"data": []})

    application = web.Application()
    application.router.add_get("/api2/json/cluster/metrics/export", handler)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets if site._server is not None else []
    assert sockets
    port = sockets[0].getsockname()[1]

    service = SERVICES["PVE"]
    auth = TokenAuth(
        user="root@pam", token_name="test", token_value="secret", service_config=service
    )
    backend = HttpsBackend(host="127.0.0.1", service_config=service, auth=auth, timeout=2)
    # Loopback aiohttp serves plain HTTP; the backend only ever builds https URLs.
    loopback = "http://127.0.0.1:" + str(port)
    backend._url_for = lambda path: loopback + path  # type: ignore[method-assign]
    resource = ProxmoxResource("/api2/json/cluster/metrics/export", backend)
    try:
        result = await bounded_proxmox_get(
            resource, 128, history=enabled, **{"local-only": enabled}
        )
    finally:
        await backend.close()
        await runner.cleanup()

    expected = "1" if enabled else "0"
    assert result == []
    assert captured == {"history": expected, "local-only": expected}
