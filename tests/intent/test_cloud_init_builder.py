"""Cloud-init Proxmox argument builder tests."""

from __future__ import annotations

from urllib.parse import quote

from proxbox_api.routes.intent.cloud_init import CloudInitPayload, build_proxmox_ci_args


def test_build_proxmox_ci_args_maps_user_ssh_keys_and_network():
    ssh_keys = [
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFirstKey alice@example",
        "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCSecondKey bob@example",
    ]
    payload = CloudInitPayload(
        user="ubuntu",
        ssh_keys=ssh_keys,
        user_data={"package_update": True, "timezone": "UTC"},
        network={"ip": "192.0.2.50", "cidr": 24, "gw": "192.0.2.1"},
    )

    args = build_proxmox_ci_args(payload)

    assert args["ciuser"] == "ubuntu"
    assert args["sshkeys"] == quote("\n".join(ssh_keys), safe="")
    assert args["ipconfig0"] == "ip=192.0.2.50/24,gw=192.0.2.1"
    assert str(args["cicustom"]).startswith("user=")
    assert "package_update: true" in str(args["cicustom"])


def test_build_proxmox_ci_args_passes_through_cicustom_string():
    payload = CloudInitPayload(user_data="user=local:snippets/vm-101.yaml")

    assert build_proxmox_ci_args(payload) == {"cicustom": "user=local:snippets/vm-101.yaml"}
