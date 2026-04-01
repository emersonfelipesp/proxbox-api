"""VM Sync Coordinator - orchestrates the full VM synchronization workflow."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from proxbox_api.cache import global_cache
from proxbox_api.constants import (
    DEFAULT_TAG_COLOR,
    DEFAULT_VM_ROLE,
)
from proxbox_api.dependencies import (
    CreateCustomFieldsDep,
    NetBoxSessionDep,
    ProxmoxSessionsDep,
    ProxboxTagDep,
)
from proxbox_api.exception import VMSyncError
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    rest_list_async,
    rest_patch_async,
    rest_reconcile_async,
)
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxDeviceRoleSyncState,
    NetBoxIpAddressSyncState,
    NetBoxVirtualDiskSyncState,
    NetBoxVirtualMachineCreateBody,
    NetBoxVirtualMachineInterfaceSyncState,
    NetBoxVlanSyncState,
    ProxmoxVmConfigInput,
)
from proxbox_api.proxmox_to_netbox.normalize import normalize_tag_refs
from proxbox_api.routes.proxmox import get_vm_config
from proxbox_api.routes.proxmox.cluster import ClusterResourcesDep, ClusterStatusDep
from proxbox_api.services.proxmox_helpers import get_qemu_guest_agent_network_interfaces
from proxbox_api.services.sync.devices import (
    _ensure_cluster,
    _ensure_cluster_type,
    _ensure_device,
    _ensure_device_type,
    _ensure_manufacturer,
    _ensure_site,
)
from proxbox_api.services.sync.devices import (
    _ensure_device_role as _ensure_proxmox_node_role,
)
from proxbox_api.services.sync.storage_links import (
    find_storage_record,
    storage_name_from_volume_id,
)
from proxbox_api.services.sync.task_history import sync_virtual_machine_task_history
from proxbox_api.services.sync.virtual_machines import build_netbox_virtual_machine_payload
from proxbox_api.services.sync.vm_helpers import (
    best_guest_agent_ip,
    normalized_mac,
    relation_id,
    relation_name,
    to_mapping,
)
from proxbox_api.utils.websocket_utils import send_error, send_progress_update


@dataclass
class VMSyncResult:
    """Result of a VM synchronization operation."""

    created: int = 0
    updated: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class VMSyncContext:
    """Context passed through the VM sync workflow."""

    netbox_session: NetBoxSessionDep
    pxs: ProxmoxSessionsDep
    cluster_status: ClusterStatusDep
    cluster_resources: ClusterResourcesDep
    custom_fields: CreateCustomFieldsDep
    tag: ProxboxTagDep
    storage_index: dict[tuple[str, str], dict] = field(default_factory=dict)
    use_websocket: bool = False
    use_guest_agent_interface_name: bool = True
    use_css: bool = False


class VMSyncCoordinator:
    """Coordinates the full VM synchronization from Proxmox to NetBox."""

    def __init__(self, context: VMSyncContext) -> None:
        self.context = context
        self._result = VMSyncResult()

    async def run(self, cluster_resources: list[dict]) -> VMSyncResult:
        """Run the full VM sync across all clusters."""
        await self._load_storage_index()
        await self._process_clusters(cluster_resources)
        global_cache.clear_cache()
        return self._result

    async def _load_storage_index(self) -> None:
        """Load storage records for disk mapping."""
        from proxbox_api.services.sync.storage_links import build_storage_index

        try:
            storage_records = await rest_list_async(
                self.context.netbox_session,
                "/api/plugins/proxbox/storage/",
            )
            self.context.storage_index = build_storage_index(storage_records)
        except Exception as error:
            logger.warning("Error loading storage records for VM sync: %s", error)

    async def _process_clusters(self, cluster_resources: list[dict]) -> None:
        """Process all clusters and their VMs."""
        max_concurrency = 4
        semaphore = asyncio.Semaphore(max_concurrency)

        async def run_vm_task(cluster_name: str, resource: dict) -> dict | Exception:
            async with semaphore:
                return await self._sync_single_vm(cluster_name, resource)

        async def process_cluster(cluster: dict) -> list:
            tasks = []
            for cluster_name, resources in cluster.items():
                for resource in resources:
                    if resource.get("type") in ("qemu", "lxc"):
                        tasks.append(run_vm_task(cluster_name, resource))
            return await asyncio.gather(*tasks, return_exceptions=True)

        results = await asyncio.gather(
            *[process_cluster(c) for c in cluster_resources],
            return_exceptions=True,
        )

        for cluster_results in results:
            if isinstance(cluster_results, Exception):
                self._result.failed += 1
                continue
            for vm_result in cluster_results:
                if isinstance(vm_result, Exception):
                    self._result.failed += 1
                    self._result.errors.append(str(vm_result))
                else:
                    self._result.created += 1

    async def _sync_single_vm(
        self,
        cluster_name: str,
        resource: dict,
    ) -> dict | Exception:
        """Sync a single VM from Proxmox to NetBox."""
        try:
            await self._sync_vm_interfaces_and_disks(cluster_name, resource)
            self._result.created += 1
            return {"name": resource.get("name"), "vmid": resource.get("vmid")}
        except Exception as e:
            self._result.failed += 1
            self._result.errors.append(f"VM {resource.get('name')}: {e}")
            return e

    async def _ensure_vm_dependencies(
        self,
        cluster_name: str,
        tag_refs: list[dict],
    ) -> tuple:
        """Ensure all VM dependencies exist (cluster, device, role, etc.)."""
        cluster_mode = next(
            (
                cluster_state.mode
                for cluster_state in self.context.cluster_status
                if getattr(cluster_state, "name", None) == cluster_name
            ),
            "cluster",
        )

        cluster_type = await _ensure_cluster_type(
            self.context.netbox_session,
            mode=cluster_mode,
            tag_refs=tag_refs,
        )
        cluster = await _ensure_cluster(
            self.context.netbox_session,
            cluster_name=cluster_name,
            cluster_type_id=getattr(cluster_type, "id", None),
            mode=cluster_mode,
            tag_refs=tag_refs,
        )
        manufacturer = await _ensure_manufacturer(
            self.context.netbox_session,
            tag_refs=tag_refs,
        )
        device_type = await _ensure_device_type(
            self.context.netbox_session,
            manufacturer_id=getattr(manufacturer, "id", None),
            tag_refs=tag_refs,
        )
        device_role = await _ensure_proxmox_node_role(
            self.context.netbox_session,
            tag_refs=tag_refs,
        )
        site = await _ensure_site(
            self.context.netbox_session,
            cluster_name=cluster_name,
            tag_refs=tag_refs,
        )
        device = await _ensure_device(
            self.context.netbox_session,
            device_name=resource.get("node"),
            cluster_id=getattr(cluster, "id", None),
            device_type_id=getattr(device_type, "id", None),
            role_id=getattr(device_role, "id", None),
            site_id=getattr(site, "id", None),
            tag_refs=tag_refs,
        )

        return cluster, device

    async def _sync_vm_interfaces_and_disks(
        self,
        cluster_name: str,
        resource: dict,
    ) -> dict:
        """Sync VM interfaces, IPs, and disks after VM creation."""
        # This is a placeholder - the actual logic needs to be extracted from sync_vm.py
        # For now, we'll keep the existing implementation in sync_vm.py and this coordinator
        # will be the orchestrator
        raise NotImplementedError("Interface and disk sync needs to be implemented in coordinator")


async def create_virtual_machines_v2(
    context: VMSyncContext,
    cluster_resources: list[dict],
) -> VMSyncResult:
    """Create virtual machines using the new coordinator pattern."""
    coordinator = VMSyncCoordinator(context)
    return await coordinator.run(cluster_resources)
