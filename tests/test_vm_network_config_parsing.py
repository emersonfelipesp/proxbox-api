"""Regression tests for sparse Proxmox VM ``net<N>`` config keys."""

from __future__ import annotations

from proxbox_api.routes.virtualization.virtual_machines.sync_vm import _parse_vm_networks
from proxbox_api.schemas.virtualization import VMConfig
from proxbox_api.services.sync.individual.helpers import extract_net_interface_config
from proxbox_api.services.sync.vm_filter import parse_network_config
from proxbox_api.services.sync.vm_helpers import (
    iter_proxmox_net_config_items,
    parse_proxmox_net_configs,
)

SPARSE_QEMU_CONFIG: dict[str, object] = {
    "name": "dc-gw-beta-node1-ah-ams3",
    "agent": "1",
    "net19": "virtio=BC:24:11:EC:FA:57,bridge=vmbr258,mtu=1,queues=4",
    "net9": "virtio=BC:24:11:93:EC:1C,bridge=vmbr248,mtu=1,queues=4",
    "net2": "virtio=BC:24:11:E8:44:8E,bridge=vmbr244,mtu=1,queues=4",
    "net1": "virtio=BC:24:11:0D:43:B8,bridge=vmbr240,mtu=1500,queues=4",
    "netboot": "should-not-be-treated-as-a-network-interface",
    "running-nets-host-mtu": "vmbr240=1500",
}


def _network_keys(networks: list[dict[str, object]]) -> list[str]:
    return [next(iter(network.keys())) for network in networks]


def test_sparse_qemu_net_keys_are_parsed_without_net0():
    """Bulk VM-interface sync must not stop just because ``net0`` is absent."""
    expected_keys = ["net1", "net2", "net9", "net19"]

    assert _network_keys(parse_proxmox_net_configs(SPARSE_QEMU_CONFIG)) == expected_keys
    assert _network_keys(_parse_vm_networks(SPARSE_QEMU_CONFIG)) == expected_keys
    assert _network_keys(parse_network_config(SPARSE_QEMU_CONFIG)) == expected_keys
    assert list(extract_net_interface_config(SPARSE_QEMU_CONFIG)) == expected_keys


def test_proxmox_net_key_iterator_excludes_net_prefix_lookalikes():
    keys = [key for key, _value in iter_proxmox_net_config_items(SPARSE_QEMU_CONFIG)]

    assert keys == ["net1", "net2", "net9", "net19"]
    assert "netboot" not in keys
    assert "running-nets-host-mtu" not in keys


def test_vm_config_schema_networks_accept_sparse_qemu_net_keys():
    config = VMConfig.model_validate(
        {
            "name": "dc-gw-beta-node1-ah-ams3",
            "memory": "8192",
            "agent": "1",
            "net10": "virtio=BC:24:11:D5:A5:3D,bridge=vmbr249",
            "net2": "virtio=BC:24:11:E8:44:8E,bridge=vmbr244",
            "net1": "virtio=BC:24:11:0D:43:B8,bridge=vmbr240",
        }
    )

    assert _network_keys(config.networks) == ["net1", "net2", "net10"]
    assert config.networks[0]["net1"]["bridge"] == "vmbr240"
