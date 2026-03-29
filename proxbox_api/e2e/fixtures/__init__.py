"""E2E test fixtures for proxbox-api."""

from proxbox_api.e2e.fixtures.proxmox_mock import (
    MockProxmoxCluster,
    MockProxmoxNode,
    MockProxmoxVM,
    create_cluster_with_backups,
    create_minimal_cluster,
    create_multi_cluster,
)
from proxbox_api.e2e.fixtures.test_data import (
    E2E_CLUSTER,
    E2E_NODE,
    E2E_TAG,
    E2E_VM_LXC,
    E2E_VM_QEMU,
    generate_unique_resource_prefix,
)

__all__ = [
    "MockProxmoxCluster",
    "MockProxmoxNode",
    "MockProxmoxVM",
    "create_cluster_with_backups",
    "create_minimal_cluster",
    "create_multi_cluster",
    "E2E_CLUSTER",
    "E2E_NODE",
    "E2E_TAG",
    "E2E_VM_LXC",
    "E2E_VM_QEMU",
    "generate_unique_resource_prefix",
]
