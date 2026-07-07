from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from proxbox_api.firecracker_agent.app import create_firecracker_agent_app
from proxbox_api.firecracker_agent.client import FirecrackerHostAgentError
from proxbox_api.routes.cloud import firecracker as firecracker_routes
from proxbox_api.schemas.firecracker import (
    FirecrackerAssetPrepareResponse,
    FirecrackerHostAgentHealth,
    FirecrackerHostCapabilities,
    FirecrackerImageBundle,
    FirecrackerMicroVMState,
    FirecrackerProvisionRequest,
)


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
    assert "host_agent_base_url" in response.text


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
