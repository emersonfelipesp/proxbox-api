"""Static and registration checks for Cloud Portal routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from proxbox_api.app import factory
from proxbox_api.routes import cloud
from proxbox_api.routes.intent.cloud_init import CloudInitPayload
from proxbox_api.schemas.cloud_provision import CloudVMProvisionRequest


def test_cloud_package_exposes_both_routers():
    assert cloud.provision_router is not None
    assert cloud.provision_stream_router is not None
    assert cloud.firecracker_router is not None
    assert cloud.image_factory_router is not None
    assert cloud.template_images_router is not None
    assert cloud.templates_router is not None
    assert cloud.pve_template_router is not None
    assert cloud.qemu_templates_router is not None
    assert cloud.versions_router is not None
    assert cloud.__all__ == (
        "provision_router",
        "provision_stream_router",
        "firecracker_router",
        "image_factory_router",
        "pve_template_router",
        "qemu_templates_router",
        "template_images_router",
        "templates_router",
        "versions_router",
    )


def test_cloud_routes_are_registered_on_app(monkeypatch):
    monkeypatch.delenv("PROXBOX_FEATURES", raising=False)

    test_app = factory.create_app()

    assert any(
        route.path == "/cloud/vm/provision" and "POST" in (route.methods or set())
        for route in test_app.routes
    )
    assert any(
        route.path == "/cloud/templates" and "GET" in (route.methods or set())
        for route in test_app.routes
    )
    assert any(
        route.path == "/cloud/templates/images" and "POST" in (route.methods or set())
        for route in test_app.routes
    )
    assert any(
        route.path == "/cloud/vm/templates" and "GET" in (route.methods or set())
        for route in test_app.routes
    )


def _valid_request_payload() -> dict[str, object]:
    return {
        "endpoint_id": 1,
        "template_vmid": 9000,
        "new_vmid": 9100,
        "new_name": "tenant-vm-9100",
        "target_node": "pve",
        "cloud_init": CloudInitPayload(user="ubuntu", ssh_keys=["ssh-rsa AAA"]),
    }


def test_cloud_provision_request_rejects_extra_fields():
    payload = _valid_request_payload()
    payload["unexpected"] = "blocked"

    with pytest.raises(ValidationError):
        CloudVMProvisionRequest.model_validate(payload)


def test_cloud_provision_request_rejects_template_vmid_below_100():
    payload = _valid_request_payload()
    payload["template_vmid"] = 99

    with pytest.raises(ValidationError):
        CloudVMProvisionRequest.model_validate(payload)


def test_cloud_provision_route_reuses_required_helpers():
    source = (
        Path(__file__).parents[2] / "proxbox_api" / "routes" / "cloud" / "provision.py"
    ).read_text(encoding="utf-8")

    assert "build_proxmox_ci_args" in source
    assert "_gate" in source
    assert "_wait_for_upid" in source
    assert "await _wait_for_upid(proxmox, template_node, clone_upid)" in source
