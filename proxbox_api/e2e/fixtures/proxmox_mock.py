"""Mock Proxmox API data structures for e2e testing.

Provides classes and factory functions to generate mock Proxmox API responses
for testing the proxbox-api sync functionality.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field



@dataclass
class MockProxmoxNode:
    """Represents a Proxmox node in the API."""

    name: str
    status: str = "online"
    uptime: int = 3600
    max_disk: int = 1073741824000
    max_mem: int = 68719476736
    level: str = ""

    def to_api_response(self) -> dict[str, object]:
        """Convert to Proxmox API response format."""
        return {
            "node": self.name,
            "status": self.status,
            "uptime": self.uptime,
            "max_disk": self.max_disk,
            "max_mem": self.max_mem,
            "level": self.level,
        }


@dataclass
class MockProxmoxVM:
    """Represents a Proxmox VM or LXC container."""

    vmid: int
    name: str
    node: str
    status: str = "running"
    type: str = "qemu"
    maxcpu: int = 2
    maxmem: int = 4294967296
    maxdisk: int = 53687091200
    config: dict[str, object] = field(default_factory=dict)

    def to_resource(self) -> dict[str, object]:
        """Convert to Proxmox cluster/resources API response."""
        return {
            "vmid": self.vmid,
            "name": self.name,
            "node": self.node,
            "status": self.status,
            "type": self.type,
            "maxcpu": self.maxcpu,
            "maxmem": self.maxmem,
            "maxdisk": self.maxdisk,
        }

    def to_config(self) -> dict[str, object]:
        """Convert to Proxmox VM config API response."""
        return {
            "onboot": self.config.get("onboot", 1),
            "agent": self.config.get("agent", 1),
            "unprivileged": self.config.get("unprivileged", 0),
            "searchdomain": self.config.get("searchdomain", "lab.local"),
        }


@dataclass
class MockProxmoxCluster:
    """Represents a Proxmox cluster with nodes and VMs."""

    name: str
    mode: str = "pve"
    nodes: list[MockProxmoxNode] = field(default_factory=list)
    vms: list[MockProxmoxVM] = field(default_factory=list)
    storage: list[dict[str, object]] = field(default_factory=list)

    def to_cluster_status(self) -> dict[str, object]:
        """Convert to cluster/status API response."""
        return {
            "name": self.name,
            "mode": self.mode,
            "node_list": [node.to_api_response() for node in self.nodes],
        }

    def to_cluster_resources(self) -> list[dict[str, object]]:
        """Convert to cluster/resources API response."""
        return [vm.to_resource() for vm in self.vms]

    def add_storage(
        self,
        storage_id: str,
        storage_type: str = "dir",
        content: str = "rootdir,images",
        shared: bool = False,
    ) -> "MockProxmoxCluster":
        """Add storage to the cluster."""
        self.storage.append(
            {
                "storage": storage_id,
                "type": storage_type,
                "content": content,
                "shared": shared,
                "nodes": "all",
            }
        )
        return self

    def add_backup_storage(
        self,
        storage_id: str = "backup",
        path: str = "/mnt/backup",
    ) -> "MockProxmoxCluster":
        """Add backup storage to the cluster."""
        self.storage.append(
            {
                "storage": storage_id,
                "type": "dir",
                "content": "backup,vztmpl,iso",
                "shared": True,
                "nodes": "all",
                "path": path,
            }
        )
        return self


class MockProxmoxBackup:
    """Represents a Proxmox VM backup."""

    def __init__(
        self,
        vmid: int,
        volid: str,
        storage: str = "backup",
        size: int = 10737418240,
        ctime: int | None = None,
        format: str = "qcow2",
        subtype: str = "private",
        notes: str = "",
    ):
        self.vmid = vmid
        self.volid = volid
        self.storage = storage
        self.size = size
        self.ctime = ctime or int(time.time())
        self.format = format
        self.subtype = subtype
        self.notes = notes

    def to_api_response(self) -> dict[str, object]:
        """Convert to Proxmox storage/content API response."""
        return {
            "vmid": self.vmid,
            "volid": self.volid,
            "storage": self.storage,
            "size": self.size,
            "ctime": self.ctime,
            "format": self.format,
            "subtype": self.subtype,
            "notes": self.notes,
            "content": "backup",
        }


def create_minimal_cluster(prefix: str = "e2e-minimal") -> MockProxmoxCluster:
    """Create a minimal cluster for e2e testing.

    Single node with 2 VMs (1 QEMU, 1 LXC).
    """
    node_name = f"{prefix}-node-01"
    cluster_name = f"{prefix}-cluster"

    cluster = MockProxmoxCluster(
        name=cluster_name,
        mode="pve",
        nodes=[
            MockProxmoxNode(
                name=node_name,
                status="online",
                uptime=3600,
            ),
        ],
        vms=[
            MockProxmoxVM(
                vmid=99901,
                name=f"{prefix}-qemu",
                node=node_name,
                status="running",
                type="qemu",
                maxcpu=2,
                maxmem=4294967296,
                maxdisk=53687091200,
                config={
                    "onboot": 1,
                    "agent": 1,
                    "unprivileged": 0,
                    "searchdomain": "lab.local",
                },
            ),
            MockProxmoxVM(
                vmid=99902,
                name=f"{prefix}-lxc",
                node=node_name,
                status="running",
                type="lxc",
                maxcpu=1,
                maxmem=2147483648,
                maxdisk=8589934592,
                config={
                    "onboot": 1,
                    "unprivileged": 1,
                },
            ),
        ],
    )
    cluster.add_storage("local", content="rootdir,images")
    cluster.add_backup_storage()
    return cluster


def create_multi_cluster(prefix: str = "e2e-multi") -> list[MockProxmoxCluster]:
    """Create multiple clusters for e2e testing.

    Two clusters, each with 2 nodes and 3 VMs.
    """
    clusters = []

    for i in range(1, 3):
        cluster_name = f"{prefix}-cluster-{i:02d}"
        cluster = MockProxmoxCluster(
            name=cluster_name,
            mode="pve",
            nodes=[
                MockProxmoxNode(
                    name=f"{prefix}-node-{i:02d}-01",
                    status="online",
                    uptime=3600 * (i * 24),
                ),
                MockProxmoxNode(
                    name=f"{prefix}-node-{i:02d}-02",
                    status="online" if i == 1 else "offline",
                    uptime=3600 * 12,
                ),
            ],
            vms=[
                MockProxmoxVM(
                    vmid=99000 + (i * 100) + 1,
                    name=f"{prefix}-vm-{i:02d}-01",
                    node=f"{prefix}-node-{i:02d}-01",
                    status="running",
                    type="qemu",
                ),
                MockProxmoxVM(
                    vmid=99000 + (i * 100) + 2,
                    name=f"{prefix}-vm-{i:02d}-02",
                    node=f"{prefix}-node-{i:02d}-01",
                    status="stopped",
                    type="qemu",
                ),
                MockProxmoxVM(
                    vmid=99000 + (i * 100) + 3,
                    name=f"{prefix}-lxc-{i:02d}-01",
                    node=f"{prefix}-node-{i:02d}-02",
                    status="running",
                    type="lxc",
                ),
            ],
        )
        cluster.add_storage(f"local-{i}", content="rootdir,images")
        cluster.add_backup_storage(storage_id=f"backup-{i}")
        clusters.append(cluster)

    return clusters


def create_cluster_with_backups(
    prefix: str = "e2e-backup",
) -> tuple[MockProxmoxCluster, list[MockProxmoxBackup]]:
    """Create a cluster with VMs that have backup metadata."""
    cluster = create_minimal_cluster(prefix)

    backups = []
    base_time = int(time.time())

    for vm in cluster.vms:
        backup = MockProxmoxBackup(
            vmid=vm.vmid,
            volid=f"backup:{vm.vmid}/vm-{vm.vmid}-disk-0.qcow2",
            storage="backup",
            size=vm.maxdisk // 10,
            ctime=base_time - 86400,
            format="qcow2",
            subtype="private",
            notes=f"Auto backup for {vm.name}",
        )
        backups.append(backup)

        extra_backup = MockProxmoxBackup(
            vmid=vm.vmid,
            volid=f"backup:{vm.vmid}/vm-{vm.vmid}-disk-0_20240115.qcow2",
            storage="backup",
            size=vm.maxdisk // 10,
            ctime=base_time - 86400 * 7,
            format="qcow2",
            subtype="private",
            notes=f"Weekly backup for {vm.name}",
        )
        backups.append(extra_backup)

    return cluster, backups


def create_custom_cluster(
    name: str,
    nodes_spec: list[tuple[str, str, int]],
    vms_spec: list[tuple[int, str, str, str, int, int, int]],
    prefix: str = "e2e",
) -> MockProxmoxCluster:
    """Create a custom cluster with specified nodes and VMs.

    Args:
        name: Cluster name.
        nodes_spec: List of (node_name, status, uptime) tuples.
        vms_spec: List of (vmid, name, node, type, maxcpu, maxmem, maxdisk) tuples.
        prefix: Prefix for resource naming.

    Returns:
        MockProxmoxCluster with specified configuration.
    """
    cluster = MockProxmoxCluster(name=name, mode="pve")

    for node_name, status, uptime in nodes_spec:
        cluster.nodes.append(
            MockProxmoxNode(
                name=node_name,
                status=status,
                uptime=uptime,
            )
        )

    for vmid, name, node, vm_type, maxcpu, maxmem, maxdisk in vms_spec:
        cluster.vms.append(
            MockProxmoxVM(
                vmid=vmid,
                name=name,
                node=node,
                type=vm_type,
                maxcpu=maxcpu,
                maxmem=maxmem,
                maxdisk=maxdisk,
            )
        )

    cluster.add_storage("local", content="rootdir,images")
    cluster.add_backup_storage()

    return cluster
