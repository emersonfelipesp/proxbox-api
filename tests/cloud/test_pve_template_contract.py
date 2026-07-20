"""Static contracts for the PVE-installer cloud-init template build route."""

from __future__ import annotations

from pathlib import Path

import pytest

from proxbox_api.routes.cloud import pve_template
from proxbox_api.routes.cloud.pve_cloudinit_payload import render_all
from proxbox_api.schemas.cloud_provision import PVETemplateBuildRequest

ROOT = Path(__file__).resolve().parents[2]


def test_cloud_pve_template_build_route_registered() -> None:
    factory = (ROOT / "proxbox_api/app/factory.py").read_text()
    init = (ROOT / "proxbox_api/routes/cloud/__init__.py").read_text()
    assert "pve_template_router" in init
    assert "cloud_pve_template_router" in factory


def test_cloud_pve_template_build_route_source_shape() -> None:
    src = (ROOT / "proxbox_api/routes/cloud/pve_template.py").read_text()
    for token in (
        '"/templates/pve"',
        "validate_endpoint_url",
        "render_all",
        "import-from=",
        "cicustom",
        "snippets/",
        "_gate",
    ):
        assert token in src, f"missing token in pve_template.py: {token!r}"
    assert '"serial0": "socket"' not in src
    assert '"vga": "serial0"' not in src


@pytest.mark.asyncio
async def test_pve_template_direct_create_uses_graphical_display(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Config:
        async def get(self):
            raise RuntimeError("missing vm")

    class _QemuVm:
        config = _Config()

    class _Qemu:
        def __call__(self, _vmid):
            return _QemuVm()

        async def post(self, **kwargs):
            captured.update(kwargs)
            return {"data": "UPID:pve:create"}

    class _Content:
        async def get(self, **_kwargs):
            return []

    class _Storage:
        content = _Content()

        async def post(self, **_kwargs):
            return {"data": "UPID:pve:download"}

    class _Node:
        qemu = _Qemu()

        def storage(self, _storage):
            return _Storage()

    class _Session:
        def nodes(self, _node):
            return _Node()

    class _Proxmox:
        session = _Session()

        async def aclose(self):
            return None

    async def _fake_gate(_session, _endpoint_id):
        return object()

    async def _fake_open(_endpoint):
        return _Proxmox()

    async def _fake_wait(_proxmox, *, node, response):
        return f"UPID:{node}:ok"

    monkeypatch.setattr(pve_template, "_gate", _fake_gate)
    monkeypatch.setattr(pve_template, "_open_proxmox_session", _fake_open)
    monkeypatch.setattr(pve_template, "_wait_for_task", _fake_wait)

    await pve_template.build_pve_template(
        req=PVETemplateBuildRequest(endpoint_id=1, target_node="pve", vmid=9302),
        session=object(),
    )

    assert captured["vga"] == "std"
    assert "serial0" not in captured


def test_pve_cloudinit_payload_renders_required_fields() -> None:
    payloads = render_all(
        hostname="pve-node-42",
        node_cidr="10.0.30.42/24",
        gateway="10.0.30.1",
        pve_version_pin="9.1.11",
        ssh_authorized_keys=["ssh-ed25519 AAAA testkey op@host"],
    )
    assert "#cloud-config" in payloads.user_data
    assert "pve-node-42.nmulti.local" in payloads.user_data
    assert "Pin: version 9.1.11*" in payloads.user_data
    assert "pve-enterprise.sources" in payloads.user_data
    assert "grub-pc/install_devices multiselect %s" in payloads.user_data
    assert "proxmox-ve postfix open-iscsi chrony" in payloads.user_data
    assert payloads.user_data.index("grub-pc/install_devices") < (
        payloads.user_data.index("proxmox-ve postfix open-iscsi chrony")
    )
    assert "ssh-ed25519 AAAA testkey op@host" in payloads.user_data
    assert "vmbr0:" in payloads.network_config
    assert "10.0.30.42/24" in payloads.network_config
    assert "stp: false" in payloads.network_config
    assert payloads.meta_data.startswith("instance-id: pve-node-42")
    assert "local-hostname: pve-node-42" in payloads.meta_data
