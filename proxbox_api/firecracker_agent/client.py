"""Async HTTP client for the Firecracker host-agent API."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any, AsyncIterator, Iterator, TypeVar
from uuid import UUID

import httpcore
import httpx
from pydantic import BaseModel, ValidationError

from proxbox_api.logger import logger
from proxbox_api.schemas.firecracker import (
    FirecrackerAssetPrepareRequest,
    FirecrackerAssetPrepareResponse,
    FirecrackerHostAgentHealth,
    FirecrackerHostCapabilities,
    FirecrackerMicroVMAction,
    FirecrackerMicroVMCreateRequest,
    FirecrackerMicroVMMetrics,
    FirecrackerMicroVMState,
)
from proxbox_api.settings_client import get_settings
from proxbox_api.ssrf import ResolvedAddress, resolve_endpoint_host


class FirecrackerHostAgentError(RuntimeError):
    """Raised when a host-agent request fails."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


_HTTPCORE_EXCEPTION_MAP: dict[type[Exception], type[httpx.HTTPError]] = {
    httpcore.TimeoutException: httpx.TimeoutException,
    httpcore.ConnectTimeout: httpx.ConnectTimeout,
    httpcore.ReadTimeout: httpx.ReadTimeout,
    httpcore.WriteTimeout: httpx.WriteTimeout,
    httpcore.PoolTimeout: httpx.PoolTimeout,
    httpcore.NetworkError: httpx.NetworkError,
    httpcore.ConnectError: httpx.ConnectError,
    httpcore.ReadError: httpx.ReadError,
    httpcore.WriteError: httpx.WriteError,
    httpcore.ProxyError: httpx.ProxyError,
    httpcore.UnsupportedProtocol: httpx.UnsupportedProtocol,
    httpcore.ProtocolError: httpx.ProtocolError,
    httpcore.LocalProtocolError: httpx.LocalProtocolError,
    httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
}


@contextmanager
def _map_httpcore_exceptions() -> Iterator[None]:
    """Translate httpcore failures at the custom transport boundary."""
    try:
        yield
    except Exception as exc:
        mapped: type[httpx.HTTPError] | None = None
        for from_exc, to_exc in _HTTPCORE_EXCEPTION_MAP.items():
            if isinstance(exc, from_exc) and (mapped is None or issubclass(to_exc, mapped)):
                mapped = to_exc
        if mapped is None:
            raise
        raise mapped(str(exc)) from exc


_HOST_AGENT_ADDRESS_CONNECT_TIMEOUT = 2.0
_HOST_AGENT_TOTAL_CONNECT_TIMEOUT = 5.0
_ResponseModelT = TypeVar("_ResponseModelT", bound=BaseModel)


def _validate_response_model(
    model_type: type[_ResponseModelT],
    payload: object,
) -> _ResponseModelT:
    """Validate an upstream payload without retaining its values on failure."""
    try:
        return model_type.model_validate(payload)
    except ValidationError as error:
        fields: set[str] = set()
        error_types: set[str] = set()
        for detail in error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        ):
            location = detail.get("loc", ())
            top_level = location[0] if location else None
            if isinstance(top_level, str) and top_level in model_type.model_fields:
                fields.add(top_level)
            else:
                fields.add("<response>")
            error_types.add(str(detail.get("type", "validation_error")))
        logger.warning(
            "Firecracker host-agent response validation failed model=%s fields=%s types=%s",
            model_type.__name__,
            ",".join(sorted(fields)),
            ",".join(sorted(error_types)),
        )
        raise FirecrackerHostAgentError("host-agent returned an invalid response") from None


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect only to addresses already accepted by the shared SSRF guard."""

    def __init__(self, hostname: str, addresses: tuple[ResolvedAddress, ...]) -> None:
        self._hostname = hostname
        self._addresses = addresses
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,  # noqa: ANN001
    ) -> httpcore.AsyncNetworkStream:
        if host != self._hostname:
            raise httpcore.ConnectError("unexpected host for pinned transport")
        loop = asyncio.get_running_loop()
        total_timeout = min(
            timeout if timeout is not None else _HOST_AGENT_TOTAL_CONNECT_TIMEOUT,
            _HOST_AGENT_TOTAL_CONNECT_TIMEOUT,
        )
        deadline = loop.time() + max(0.001, total_timeout)
        last_error: Exception | None = None
        for address in self._addresses:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                return await self._backend.connect_tcp(
                    address.httpcore_host,
                    address.port if address.port is not None else port,
                    timeout=min(_HOST_AGENT_ADDRESS_CONNECT_TIMEOUT, remaining),
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (OSError, httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if isinstance(last_error, httpcore.ConnectTimeout):
            raise httpcore.ConnectTimeout(
                "no validated host-agent address was reachable"
            ) from last_error
        raise httpcore.ConnectError("no validated host-agent address was reachable") from last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options=None,  # noqa: ANN001, ARG002
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.UnsupportedProtocol("Unix sockets are disabled for host-agent requests")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        with _map_httpcore_exceptions():
            async for part in self._stream:
                yield part

    async def aclose(self) -> None:
        await self._stream.aclose()


class _PinnedHTTPTransport(httpx.AsyncBaseTransport):
    """Keep the original URL identity while replacing DNS with validated IPs."""

    def __init__(self, hostname: str, addresses: tuple[ResolvedAddress, ...]) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=True),
            max_connections=1,
            max_keepalive_connections=0,
            network_backend=_PinnedNetworkBackend(hostname, addresses),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        with _map_httpcore_exceptions():
            response = await self._pool.handle_async_request(
                httpcore.Request(
                    method=request.method,
                    url=httpcore.URL(
                        scheme=request.url.raw_scheme,
                        host=request.url.raw_host,
                        port=request.url.port,
                        target=request.url.raw_path,
                    ),
                    headers=request.headers.raw,
                    content=request.stream,
                    extensions=request.extensions,
                )
            )
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_PinnedResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


class FirecrackerHostAgentClient:
    """Small token-aware client for a single Firecracker host-agent."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        canonical_url = httpx.URL(base_url.rstrip("/"))
        self.base_url = str(canonical_url).rstrip("/")
        self.token = token
        self.timeout = timeout
        self._hostname = canonical_url.raw_host.decode("ascii")
        self._validated_addresses: tuple[ResolvedAddress, ...] | None = None
        self._address_lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    async def _resolve_validated_addresses(self) -> tuple[ResolvedAddress, ...]:
        if self._validated_addresses is not None:
            return self._validated_addresses
        async with self._address_lock:
            if self._validated_addresses is not None:
                return self._validated_addresses
            settings = await asyncio.to_thread(get_settings)
            safe, _reason, addresses = await asyncio.to_thread(
                resolve_endpoint_host,
                self._hostname,
                settings,
            )
            if not safe or not addresses:
                raise FirecrackerHostAgentError("host-agent target rejected")
            self._validated_addresses = addresses
            return addresses

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        url = f"{self.base_url}{path}"
        addresses = await self._resolve_validated_addresses()
        transport = _PinnedHTTPTransport(self._hostname, addresses)
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=transport,
                trust_env=False,
            ) as client:
                response = await client.request(
                    method,
                    url,
                    json=json,
                    headers=self._headers(),
                )
        except httpx.HTTPError as error:
            logger.warning(
                "Firecracker host-agent transport failed for %s %s (%s)",
                method,
                path,
                type(error).__name__,
            )
            raise FirecrackerHostAgentError("host-agent request failed") from error

        if response.status_code >= 400:
            logger.warning(
                "Firecracker host-agent returned HTTP %d for %s %s",
                response.status_code,
                method,
                path,
            )
            raise FirecrackerHostAgentError(
                "host-agent request failed",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError:
            raise FirecrackerHostAgentError("host-agent returned invalid JSON") from None
        if not isinstance(payload, (dict, list)):
            raise FirecrackerHostAgentError("host-agent response was not JSON object/list")
        return payload

    async def health(self) -> FirecrackerHostAgentHealth:
        payload = await self._request("GET", "/health")
        return _validate_response_model(FirecrackerHostAgentHealth, payload)

    async def capabilities(self) -> FirecrackerHostCapabilities:
        payload = await self._request("GET", "/capabilities")
        return _validate_response_model(FirecrackerHostCapabilities, payload)

    async def prepare_assets(
        self,
        request: FirecrackerAssetPrepareRequest,
    ) -> FirecrackerAssetPrepareResponse:
        payload = await self._request(
            "POST",
            "/assets/prepare",
            json=request.model_dump(mode="json"),
        )
        return _validate_response_model(FirecrackerAssetPrepareResponse, payload)

    async def create_microvm(
        self,
        request: FirecrackerMicroVMCreateRequest,
    ) -> FirecrackerMicroVMState:
        payload = await self._request(
            "POST",
            "/microvms",
            json=request.model_dump(mode="json"),
        )
        return _validate_response_model(FirecrackerMicroVMState, payload)

    async def action(
        self,
        microvm_id: UUID,
        action: FirecrackerMicroVMAction,
    ) -> FirecrackerMicroVMState:
        payload = await self._request(
            "POST",
            f"/microvms/{microvm_id}/actions/{action.value}",
        )
        return _validate_response_model(FirecrackerMicroVMState, payload)

    async def metrics(self, microvm_id: UUID) -> FirecrackerMicroVMMetrics:
        payload = await self._request("GET", f"/microvms/{microvm_id}/metrics")
        return _validate_response_model(FirecrackerMicroVMMetrics, payload)

    async def logs(self, microvm_id: UUID, *, tail: int = 200) -> list[str]:
        payload = await self._request("GET", f"/microvms/{microvm_id}/logs?tail={tail}")
        if isinstance(payload, dict):
            lines = payload.get("lines", [])
        else:
            lines = payload
        return [str(line) for line in lines]
