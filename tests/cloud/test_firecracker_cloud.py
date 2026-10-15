from __future__ import annotations

import json
import logging
import socket
from types import SimpleNamespace
from uuid import uuid4

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient

from proxbox_api.firecracker_agent import client as firecracker_client
from proxbox_api.firecracker_agent.app import create_firecracker_agent_app
from proxbox_api.firecracker_agent.client import FirecrackerHostAgentError
from proxbox_api.logger import logger
from proxbox_api.routes.cloud import firecracker as firecracker_routes
from proxbox_api.schemas.firecracker import (
    FirecrackerAssetPrepareResponse,
    FirecrackerHostAgentHealth,
    FirecrackerHostCapabilities,
    FirecrackerImageBundle,
    FirecrackerMicroVMState,
    FirecrackerProvisionRequest,
)
from proxbox_api.ssrf import ResolvedAddress


def _image() -> FirecrackerImageBundle:
    return FirecrackerImageBundle(
        image_id=7,
        name="Alpine Firecracker",
        kernel_image_url="https://images.example.test/vmlinux",
        kernel_image_sha256="a" * 64,
        rootfs_image_url="https://images.example.test/rootfs.ext4",
        rootfs_image_sha256="b" * 64,
    )


def _request() -> FirecrackerProvisionRequest:
    return FirecrackerProvisionRequest(
        host_agent_base_url="http://93.184.216.34:9090",
        host_agent_token="secret",
        host_id=11,
        host_pool_id=3,
        image=_image(),
        netbox_microvm_id=99,
        microvm_id=uuid4(),
        name="tenant-fc-01",
        tenant_id=42,
        ssh_authorized_keys=["ssh-ed25519 AAAA"],
    )


def test_development_host_agent_contract_handles_lifecycle():
    client = TestClient(create_firecracker_agent_app())

    assert client.get("/health").json()["ok"] is True
    assert client.get("/capabilities").json()["supports_nat"] is True

    prepare_response = client.post(
        "/assets/prepare",
        json={"image": _image().model_dump(mode="json")},
    )
    assert prepare_response.status_code == 200
    assert prepare_response.json()["kernel_ready"] is True

    microvm_id = str(uuid4())
    create_response = client.post(
        "/microvms",
        json={
            "microvm_id": microvm_id,
            "name": "tenant-fc-01",
            "image": _image().model_dump(mode="json"),
            "vcpus": 1,
            "memory_mib": 512,
            "disk_mib": 1024,
        },
    )
    assert create_response.status_code == 200
    assert create_response.json()["status"] == "created"

    start_response = client.post(f"/microvms/{microvm_id}/actions/start")
    assert start_response.status_code == 200
    assert start_response.json()["status"] == "running"


@pytest.mark.asyncio
async def test_firecracker_provision_stream_emits_complete(monkeypatch):
    class FakeHostAgentClient:
        def __init__(self, base_url: str, *, token: str | None = None) -> None:
            self.base_url = base_url
            self.token = token

        async def health(self) -> FirecrackerHostAgentHealth:
            return FirecrackerHostAgentHealth()

        async def capabilities(self) -> FirecrackerHostCapabilities:
            return FirecrackerHostCapabilities(
                supports_nat=True,
                supports_bridge=True,
                available_vcpus=8,
                available_memory_mib=8192,
                available_disk_mib=10240,
            )

        async def prepare_assets(self, request):
            return FirecrackerAssetPrepareResponse(
                kernel_image_path="/var/lib/firecracker/vmlinux",
                rootfs_image_path="/var/lib/firecracker/rootfs.ext4",
            )

        async def create_microvm(self, request):
            return FirecrackerMicroVMState(
                microvm_id=request.microvm_id,
                name=request.name,
                status="created",
                network_mode=request.network.mode,
                vcpus=request.vcpus,
                memory_mib=request.memory_mib,
                disk_mib=request.disk_mib,
            )

        async def action(self, microvm_id, action):
            return FirecrackerMicroVMState(
                microvm_id=microvm_id,
                name="tenant-fc-01",
                status="running",
            )

    monkeypatch.setattr(
        firecracker_routes,
        "FirecrackerHostAgentClient",
        FakeHostAgentClient,
    )

    chunks = [
        chunk
        async for chunk in firecracker_routes._firecracker_provision_stream_generator(
            _request(),
            actor="pytest",
        )
    ]

    assert any("event: provision_step" in chunk for chunk in chunks)
    complete = [chunk for chunk in chunks if "event: complete" in chunk][-1]
    payload = json.loads(complete.split("data: ", 1)[1])
    assert payload["ok"] is True
    assert payload["instance_ref"] == "firecracker:99"
    assert payload["status"] == "running"


def test_firecracker_request_rejects_extra_fields():
    payload = _request().model_dump(mode="json")
    payload["unexpected"] = True

    with pytest.raises(ValueError):
        FirecrackerProvisionRequest.model_validate(payload)


@pytest.mark.parametrize(
    "host_agent_base_url",
    [
        "ftp://93.184.216.34:9090",
        "http://127.0.0.1:9090",
        "http://169.254.169.254/latest/meta-data",
        "http://user:secret@93.184.216.34:9090",
        "http://93.184.216.34:9090?target=/health",
    ],
)
def test_firecracker_request_rejects_untrusted_host_agent_base_url(
    host_agent_base_url: str,
):
    payload = _request().model_dump(mode="json")
    payload["host_agent_base_url"] = host_agent_base_url

    with pytest.raises(ValueError, match="host_agent_base_url"):
        FirecrackerProvisionRequest.model_validate(payload)


def test_firecracker_route_rejects_ssrf_host_agent_base_url(auth_test_client, monkeypatch):
    class ForbiddenHostAgentClient:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("route must reject SSRF target before creating host-agent client")

    monkeypatch.setattr(
        firecracker_routes,
        "FirecrackerHostAgentClient",
        ForbiddenHostAgentClient,
    )
    payload = _request().model_dump(mode="json")
    payload["host_agent_base_url"] = "http://127.0.0.1:9090"

    response = auth_test_client.post("/cloud/firecracker/provision", json=payload)

    assert response.status_code == 422
    assert "request_validation_error" in response.text
    assert "host_agent_base_url" not in response.text


@pytest.mark.parametrize(
    "path", ["/cloud/firecracker/provision", "/cloud/firecracker/provision/stream"]
)
def test_firecracker_mutations_require_actor(auth_test_client, path):
    response = auth_test_client.post(path, json=_request().model_dump(mode="json"))

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "actor_required"


def test_firecracker_non_stream_error_handler_is_sanitized(auth_test_client, monkeypatch):
    class FailingHostAgentClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def health(self):
            raise FirecrackerHostAgentError("token=host-secret upstream-body-sentinel")

    monkeypatch.setattr(
        firecracker_routes,
        "FirecrackerHostAgentClient",
        FailingHostAgentClient,
    )

    response = auth_test_client.post(
        "/cloud/firecracker/provision",
        json=_request().model_dump(mode="json"),
        headers={"X-Proxbox-Actor": "pytest"},
    )

    assert response.status_code == 502
    assert response.json() == {
        "message": "Firecracker host-agent request failed.",
        "detail": "The Firecracker host-agent request could not be completed safely.",
        "python_exception": None,
    }
    assert "host-secret" not in response.text
    assert "upstream-body-sentinel" not in response.text


class _MalformedHealthHTTPClient:
    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def request(self, method, url, **_kwargs):
        return httpx.Response(
            200,
            json={"ok": "upstream-token-sentinel"},
            request=httpx.Request(method, url),
        )


def _attach_raw_test_logs(caplog, tmp_path):
    caplog.set_level(logging.WARNING, logger="proxbox")
    logger.addHandler(caplog.handler)
    path = tmp_path / "firecracker-test.log"
    file_handler = logging.FileHandler(path, encoding="utf-8")
    logger.addHandler(file_handler)
    return path, file_handler


def _detach_raw_test_logs(file_handler, caplog) -> None:
    logger.removeHandler(file_handler)
    file_handler.close()
    logger.removeHandler(caplog.handler)


def test_malformed_host_agent_200_is_sanitized_at_rest_boundary(
    auth_test_client,
    monkeypatch,
    caplog,
    tmp_path,
):
    monkeypatch.setattr(httpx, "AsyncClient", _MalformedHealthHTTPClient)
    log_path, file_handler = _attach_raw_test_logs(caplog, tmp_path)
    try:
        response = auth_test_client.post(
            "/cloud/firecracker/provision",
            json=_request().model_dump(mode="json"),
            headers={"X-Proxbox-Actor": "pytest"},
        )
    finally:
        _detach_raw_test_logs(file_handler, caplog)

    assert response.status_code == 502
    assert response.json() == {
        "message": "Firecracker host-agent request failed.",
        "detail": "The Firecracker host-agent request could not be completed safely.",
        "python_exception": None,
    }
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "upstream-token-sentinel" not in response.text
    assert "upstream-token-sentinel" not in logs
    assert "upstream-token-sentinel" not in log_path.read_text(encoding="utf-8")
    assert "fields=ok" in logs
    assert "types=bool_parsing" in logs


@pytest.mark.parametrize("diagnostics", [False, True])
def test_malformed_host_agent_200_is_sanitized_in_sse_with_any_diagnostics_setting(
    auth_test_client,
    monkeypatch,
    caplog,
    tmp_path,
    diagnostics,
):
    monkeypatch.setenv("PROXBOX_EXPOSE_INTERNAL_ERRORS", "true" if diagnostics else "false")
    monkeypatch.setattr(httpx, "AsyncClient", _MalformedHealthHTTPClient)
    log_path, file_handler = _attach_raw_test_logs(caplog, tmp_path)
    try:
        response = auth_test_client.post(
            "/cloud/firecracker/provision/stream",
            json=_request().model_dump(mode="json"),
            headers={"X-Proxbox-Actor": "pytest"},
        )
    finally:
        _detach_raw_test_logs(file_handler, caplog)

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert response.status_code == 200
    assert "upstream-token-sentinel" not in response.text
    assert "upstream-token-sentinel" not in logs
    assert "upstream-token-sentinel" not in log_path.read_text(encoding="utf-8")
    if diagnostics:
        assert "host-agent returned an invalid response" in response.text
    else:
        assert "An unexpected error occurred." in response.text


@pytest.mark.asyncio
async def test_unhealthy_host_agent_status_is_not_used_in_failure_detail(monkeypatch):
    class UnhealthyHostAgentClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def health(self):
            return FirecrackerHostAgentHealth(ok=False, status="upstream-token-sentinel")

    monkeypatch.setenv("PROXBOX_EXPOSE_INTERNAL_ERRORS", "true")
    monkeypatch.setattr(
        firecracker_routes,
        "FirecrackerHostAgentClient",
        UnhealthyHostAgentClient,
    )

    chunks = [
        chunk
        async for chunk in firecracker_routes._firecracker_provision_stream_generator(
            _request(),
            actor="pytest",
        )
    ]

    complete = [chunk for chunk in chunks if "event: complete" in chunk][-1]
    assert "upstream-token-sentinel" not in complete
    assert "host-agent health check failed" in complete


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "payload"),
    [
        ("health", {"ok": "upstream-token-sentinel"}),
        ("capabilities", {"available_vcpus": "upstream-token-sentinel"}),
        ("prepare_assets", {"kernel_image_path": "upstream-token-sentinel"}),
        ("create_microvm", {"microvm_id": "upstream-token-sentinel"}),
        ("action", {"microvm_id": "upstream-token-sentinel"}),
        ("metrics", {"microvm_id": "upstream-token-sentinel"}),
    ],
)
async def test_every_model_returning_client_method_sanitizes_validation_failure(
    monkeypatch,
    caplog,
    method_name,
    payload,
):
    async def malformed_request(*_args, **_kwargs):
        return payload

    client = firecracker_client.FirecrackerHostAgentClient("https://agent.example.test")
    monkeypatch.setattr(client, "_request", malformed_request)
    caplog.set_level(logging.WARNING, logger="proxbox")
    logger.addHandler(caplog.handler)
    request = SimpleNamespace(model_dump=lambda **_kwargs: {})
    microvm_id = uuid4()
    try:
        if method_name == "health":
            invocation = client.health()
        elif method_name == "capabilities":
            invocation = client.capabilities()
        elif method_name == "prepare_assets":
            invocation = client.prepare_assets(request)
        elif method_name == "create_microvm":
            invocation = client.create_microvm(request)
        elif method_name == "action":
            invocation = client.action(
                microvm_id,
                firecracker_client.FirecrackerMicroVMAction.start,
            )
        else:
            invocation = client.metrics(microvm_id)
        with pytest.raises(FirecrackerHostAgentError) as caught:
            await invocation
    finally:
        logger.removeHandler(caplog.handler)

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert str(caught.value) == "host-agent returned an invalid response"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "upstream-token-sentinel" not in logs


@pytest.mark.asyncio
async def test_firecracker_client_never_reflects_upstream_error_body(monkeypatch):
    class FakeHTTPClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, url, **_kwargs):
            return httpx.Response(
                500,
                text="token=host-secret upstream-body-sentinel",
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeHTTPClient)
    client = firecracker_routes.FirecrackerHostAgentClient(
        "http://93.184.216.34:9090",
        token="host-secret",
    )

    with pytest.raises(FirecrackerHostAgentError) as caught:
        await client.health()

    assert str(caught.value) == "host-agent request failed"
    assert "host-secret" not in str(caught.value)
    assert "upstream-body-sentinel" not in str(caught.value)


@pytest.mark.asyncio
async def test_firecracker_client_pins_the_first_validated_dns_result(monkeypatch):
    resolutions = 0
    transports = []

    def changing_dns(*_args, **_kwargs):
        nonlocal resolutions
        resolutions += 1
        address = "8.8.8.8" if resolutions == 1 else "169.254.169.254"
        return [(2, 1, 6, "", (address, 0))]

    class FakeTransport:
        def __init__(self, hostname, addresses):
            transports.append((hostname, addresses))

    class FakeHTTPClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, url, **_kwargs):
            return httpx.Response(
                200,
                json={"ok": True, "status": "ready"},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(firecracker_client, "get_settings", lambda: {"allow_private_ips": False})
    monkeypatch.setattr("proxbox_api.ssrf.get_registered_endpoints", lambda: (set(), set()))
    monkeypatch.setattr("proxbox_api.ssrf.socket.getaddrinfo", changing_dns)
    monkeypatch.setattr(firecracker_client, "_PinnedHTTPTransport", FakeTransport)
    monkeypatch.setattr(httpx, "AsyncClient", FakeHTTPClient)
    client = firecracker_client.FirecrackerHostAgentClient("https://agent.example.test:9090")

    await client.health()
    await client.health()

    assert resolutions == 1
    assert transports == [
        ("agent.example.test", (ResolvedAddress(socket.AF_INET, "8.8.8.8"),)),
        ("agent.example.test", (ResolvedAddress(socket.AF_INET, "8.8.8.8"),)),
    ]


@pytest.mark.asyncio
async def test_firecracker_pinned_backend_never_connects_to_original_hostname(monkeypatch):
    connections = []

    class FakeBackend:
        async def connect_tcp(self, host, port, **kwargs):
            connections.append((host, port, kwargs))
            return object()

    backend = firecracker_client._PinnedNetworkBackend(
        "agent.example.test",
        (ResolvedAddress(socket.AF_INET, "8.8.8.8"),),
    )
    backend._backend = FakeBackend()

    await backend.connect_tcp("agent.example.test", 9090)

    assert connections[0][:2] == ("8.8.8.8", 9090)


@pytest.mark.asyncio
async def test_firecracker_pinned_backend_preserves_ipv6_scope(monkeypatch):
    connections = []

    class FakeBackend:
        async def connect_tcp(self, host, port, **kwargs):
            connections.append((host, port, kwargs))
            return object()

    backend = firecracker_client._PinnedNetworkBackend(
        "agent.example.test",
        (ResolvedAddress(socket.AF_INET6, "fe80::1", scope_id=7),),
    )
    backend._backend = FakeBackend()

    await backend.connect_tcp("agent.example.test", 9090)

    assert connections[0][:2] == ("fe80::1%7", 9090)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "canonical_host"),
    [
        ("https://bücher.example:9090", "xn--bcher-kva.example"),
        ("https://xn--bcher-kva.example:9090", "xn--bcher-kva.example"),
        ("https://BÜCHER.Example:9090", "xn--bcher-kva.example"),
        ("https://bücher.example.:9090", "xn--bcher-kva.example."),
    ],
)
async def test_firecracker_custom_transport_uses_one_canonical_idna_identity(
    monkeypatch,
    base_url,
    canonical_host,
):
    resolved_hosts = []
    presented_hosts = []
    connected_hosts = []

    class FakeBackend:
        async def connect_tcp(self, host, port, **_kwargs):
            connected_hosts.append((host, port))
            return object()

    class ResponseStream:
        async def __aiter__(self):
            yield b'{"ok":true,"status":"ready"}'

        async def aclose(self):
            pass

    class IdentityCheckingPool:
        def __init__(self, *, network_backend, **_kwargs):
            self._network_backend = network_backend

        async def handle_async_request(self, request):
            presented = request.url.host.decode("ascii")
            presented_hosts.append(presented)
            await self._network_backend.connect_tcp(presented, request.url.port)
            return httpcore.Response(
                200,
                headers=[(b"content-type", b"application/json")],
                content=ResponseStream(),
            )

        async def aclose(self):
            pass

    def resolve(host, _settings):
        resolved_hosts.append(host)
        return True, "OK", (ResolvedAddress(socket.AF_INET, "8.8.8.8"),)

    monkeypatch.setattr(httpcore, "AnyIOBackend", FakeBackend)
    monkeypatch.setattr(httpcore, "AsyncConnectionPool", IdentityCheckingPool)
    monkeypatch.setattr(firecracker_client, "resolve_endpoint_host", resolve)
    monkeypatch.setattr(firecracker_client, "get_settings", lambda: {})
    client = firecracker_client.FirecrackerHostAgentClient(base_url)

    health = await client.health()

    assert health.ok is True
    assert resolved_hosts == [canonical_host]
    assert presented_hosts == [canonical_host]
    assert connected_hosts == [("8.8.8.8", 9090)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_error",
    [OSError("refused"), httpcore.ConnectTimeout("timed out")],
)
async def test_firecracker_pinned_backend_falls_back_after_transport_failure(first_error):
    connections = []
    reachable_stream = object()

    class FakeBackend:
        async def connect_tcp(self, host, port, **kwargs):
            connections.append((host, port, kwargs["timeout"]))
            if len(connections) == 1:
                raise first_error
            return reachable_stream

    backend = firecracker_client._PinnedNetworkBackend(
        "agent.example.test",
        (
            ResolvedAddress(socket.AF_INET, "8.8.8.8"),
            ResolvedAddress(socket.AF_INET, "1.1.1.1"),
        ),
    )
    backend._backend = FakeBackend()

    result = await backend.connect_tcp("agent.example.test", 9090, timeout=20.0)

    assert result is reachable_stream
    assert [entry[0] for entry in connections] == ["8.8.8.8", "1.1.1.1"]
    assert all(0 < entry[2] <= 2.0 for entry in connections)


@pytest.mark.parametrize(
    "transport_failure",
    [
        httpcore.ConnectError("address-sentinel"),
        httpcore.ConnectTimeout("address-sentinel"),
        httpcore.RemoteProtocolError("address-sentinel"),
    ],
)
def test_firecracker_transport_failures_are_sanitized_at_rest_boundary(
    auth_test_client,
    monkeypatch,
    transport_failure,
):
    class FailingPool:
        def __init__(self, **_kwargs):
            pass

        async def handle_async_request(self, _request):
            raise transport_failure

        async def aclose(self):
            pass

    monkeypatch.setattr(httpcore, "AsyncConnectionPool", FailingPool)

    response = auth_test_client.post(
        "/cloud/firecracker/provision",
        json=_request().model_dump(mode="json"),
        headers={"X-Proxbox-Actor": "pytest"},
    )

    assert response.status_code == 502
    assert response.json() == {
        "message": "Firecracker host-agent request failed.",
        "detail": "The Firecracker host-agent request could not be completed safely.",
        "python_exception": None,
    }
    assert "address-sentinel" not in response.text
    assert "93.184.216.34" not in response.text


def test_firecracker_stream_read_failure_is_sanitized_at_rest_boundary(
    auth_test_client,
    monkeypatch,
):
    class FailingStream:
        async def __aiter__(self):
            raise httpcore.ReadError("address-sentinel")
            yield b""  # pragma: no cover

        async def aclose(self):
            pass

    class ReadFailingPool:
        def __init__(self, **_kwargs):
            pass

        async def handle_async_request(self, _request):
            return httpcore.Response(200, content=FailingStream())

        async def aclose(self):
            pass

    monkeypatch.setattr(httpcore, "AsyncConnectionPool", ReadFailingPool)

    response = auth_test_client.post(
        "/cloud/firecracker/provision",
        json=_request().model_dump(mode="json"),
        headers={"X-Proxbox-Actor": "pytest"},
    )

    assert response.status_code == 502
    assert "address-sentinel" not in response.text
    assert "93.184.216.34" not in response.text


@pytest.mark.asyncio
async def test_firecracker_provision_stream_sanitizes_upstream_error(monkeypatch):
    class FailingHostAgentClient:
        def __init__(self, base_url: str, *, token: str | None = None) -> None:
            self.base_url = base_url
            self.token = token

        async def health(self) -> FirecrackerHostAgentHealth:
            raise FirecrackerHostAgentError(
                "traceback: /var/lib/firecracker/internal.py token=host-secret"
            )

    monkeypatch.delenv("PROXBOX_EXPOSE_INTERNAL_ERRORS", raising=False)
    monkeypatch.setattr(
        firecracker_routes,
        "FirecrackerHostAgentClient",
        FailingHostAgentClient,
    )

    chunks = [
        chunk
        async for chunk in firecracker_routes._firecracker_provision_stream_generator(
            _request(),
            actor="pytest",
        )
    ]

    complete = [chunk for chunk in chunks if "event: complete" in chunk][-1]
    payload = json.loads(complete.split("data: ", 1)[1])
    assert payload == {"ok": False, "error": "An unexpected error occurred."}
    assert "host-secret" not in complete
    assert "/var/lib/firecracker" not in complete
