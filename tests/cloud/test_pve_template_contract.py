"""Static contracts for the PVE-installer cloud-init template build route."""

from __future__ import annotations

from pathlib import Path

from proxbox_api.routes.cloud.pve_cloudinit_payload import render_all

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
