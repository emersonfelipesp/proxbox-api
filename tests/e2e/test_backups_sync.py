"""E2E tests for Proxmox backup synchronization.

Tests the synchronization of Proxmox VM backups to NetBox,
verifying that all synced objects have the 'proxbox e2e testing' tag.
"""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api.e2e.fixtures.proxmox_openapi_mock import (
    create_cluster_with_backups,
)
from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import (
    nested_tag_payload,
    rest_reconcile_async,
)
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxBackupSyncState,
    NetBoxClusterSyncState,
    NetBoxClusterTypeSyncState,
    NetBoxDeviceRoleSyncState,
    NetBoxDeviceSyncState,
    NetBoxDeviceTypeSyncState,
    NetBoxManufacturerSyncState,
    NetBoxSiteSyncState,
    NetBoxStorageSyncState,
    NetBoxVirtualMachineCreateBody,
    _relation_id,
)
from proxbox_api.services.sync.device_ensure import _slugify
from proxbox_api.services.sync.virtual_machines import build_netbox_virtual_machine_payload


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.mock_backend
@pytest.mark.mock_http
class TestBackupsSync:
    """E2E tests for backup synchronization."""

    async def test_sync_vm_backups_with_e2e_tag(
        self,
        netbox_e2e_session,
        e2e_tag,
        unique_prefix,
    ):
        """Test syncing VM backups with e2e tag.

        Verifies:
        1. Backup exists in NetBox
        2. Backup has 'proxbox e2e testing' tag
        3. Backup is linked to correct VM
        """
        nb = netbox_e2e_session
        tag_refs = nested_tag_payload(e2e_tag)
        cluster, backups = create_cluster_with_backups(prefix=unique_prefix)

        vm = cluster.vms[0]

        await self._setup_cluster_dependencies(nb, cluster, tag_refs)

        cluster_obj = await rest_reconcile_async(
            nb,
            "/api/virtualization/clusters/",
            lookup={"name": cluster.name},
            payload={
                "name": cluster.name,
                "type": (await self._get_cluster_type(nb, cluster.mode, tag_refs)).id,
                "description": f"Proxmox {cluster.mode} cluster.",
                "tags": tag_refs,
            },
            schema=NetBoxClusterSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "type": record.get("type"),
                "description": record.get("description"),
                "tags": record.get("tags"),
            },
        )

        # Create storages after cluster exists (storages need cluster_id FK)
        storage_lookup = await self._setup_cluster_storages(nb, cluster, cluster_obj, tag_refs)

        device = await self._get_or_create_device(
            nb, cluster, cluster_obj, cluster.nodes[0].name, tag_refs
        )

        netbox_vm_payload = build_netbox_virtual_machine_payload(
            proxmox_resource=vm.to_resource(),
            proxmox_config=vm.to_config(),
            cluster_id=cluster_obj.id,
            device_id=device.id,
            role_id=1,
            tag_ids=[e2e_tag["id"]],
        )

        vm_role = await rest_reconcile_async(
            nb,
            "/api/dcim/device-roles/",
            lookup={"slug": "virtual-machine-qemu"},
            payload={
                "name": "Virtual Machine (QEMU)",
                "slug": "virtual-machine-qemu",
                "color": "00ffff",
                "description": "Proxmox Virtual Machine",
                "tags": tag_refs,
            },
            schema=NetBoxDeviceRoleSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "slug": record.get("slug"),
                "color": record.get("color"),
                "description": record.get("description"),
                "tags": record.get("tags"),
            },
        )

        netbox_vm_payload["role"] = vm_role.id

        virtual_machine = await rest_reconcile_async(
            nb,
            "/api/virtualization/virtual-machines/",
            lookup={"name": vm.name},
            payload=netbox_vm_payload,
            schema=NetBoxVirtualMachineCreateBody,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "status": record.get("status"),
                "cluster": record.get("cluster"),
                "device": record.get("device"),
                "role": record.get("role"),
                "vcpus": record.get("vcpus"),
                "memory": record.get("memory"),
                "disk": record.get("disk"),
                "tags": record.get("tags"),
                "custom_fields": record.get("custom_fields"),
                "description": record.get("description"),
            },
        )

        vm_backups = [b for b in backups if b.vmid == vm.vmid]
        assert len(vm_backups) > 0, "Should have backups for the VM"

        created_backups = []
        for backup in vm_backups:
            storage = storage_lookup[backup.storage]
            try:
                netbox_backup = await rest_reconcile_async(
                    nb,
                    "/api/plugins/proxbox/backups/",
                    lookup={"volume_id": backup.volid},
                    payload={
                        "storage": backup.storage,
                        "virtual_machine": virtual_machine.id,
                        "subtype": vm.type,
                        "creation_time": None,
                        "size": backup.size,
                        "verification_state": None,
                        "verification_upid": None,
                        "volume_id": backup.volid,
                        "notes": backup.notes,
                        "vmid": backup.vmid,
                        "format": backup.format,
                        "tags": tag_refs,
                    },
                    schema=NetBoxBackupSyncState,
                    current_normalizer=lambda record: {
                        "storage": record.get("storage"),
                        "virtual_machine": record.get("virtual_machine"),
                        "subtype": record.get("subtype"),
                        "creation_time": record.get("creation_time"),
                        "size": record.get("size"),
                        "verification_state": record.get("verification_state"),
                        "verification_upid": record.get("verification_upid"),
                        "volume_id": record.get("volume_id"),
                        "notes": record.get("notes"),
                        "vmid": record.get("vmid"),
                        "format": record.get("format"),
                        "tags": record.get("tags"),
                    },
                )
            except ProxboxException as error:
                raise AssertionError(
                    f"backup create failed for {backup.volid}: {error.detail}"
                ) from error
            created_backups.append(netbox_backup)

        assert len(created_backups) == len(vm_backups)

        for backup in created_backups:
            backup_data = backup.serialize()
            tag_slugs = [t.get("slug") for t in backup_data.get("tags", [])]
            assert "proxbox-e2e-testing" in tag_slugs, f"Backup {backup.id} missing e2e tag"
            assert _relation_id(backup_data.get("virtual_machine")) == virtual_machine.id

    async def test_sync_multiple_vm_backups(
        self,
        netbox_e2e_session,
        e2e_tag,
        unique_prefix,
    ):
        """Test syncing backups for multiple VMs.

        Verifies that all VM backups are synced with e2e tags.
        """
        import asyncio

        nb = netbox_e2e_session
        tag_refs = nested_tag_payload(e2e_tag)
        cluster, all_backups = create_cluster_with_backups(prefix=unique_prefix)

        await self._setup_cluster_dependencies(nb, cluster, tag_refs)

        cluster_obj = await rest_reconcile_async(
            nb,
            "/api/virtualization/clusters/",
            lookup={"name": cluster.name},
            payload={
                "name": cluster.name,
                "type": (await self._get_cluster_type(nb, cluster.mode, tag_refs)).id,
                "description": f"Proxmox {cluster.mode} cluster.",
                "tags": tag_refs,
            },
            schema=NetBoxClusterSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "type": record.get("type"),
                "description": record.get("description"),
                "tags": record.get("tags"),
            },
        )

        # Create storages after cluster exists (storages need cluster_id FK)
        storage_lookup = await self._setup_cluster_storages(nb, cluster, cluster_obj, tag_refs)

        device = await self._get_or_create_device(
            nb, cluster, cluster_obj, cluster.nodes[0].name, tag_refs
        )

        async def _create_vm_with_backups(vm) -> list[Any]:
            vm_role_slug = "virtual-machine-qemu" if vm.type == "qemu" else "container-lxc"
            vm_role_name = "Virtual Machine (QEMU)" if vm.type == "qemu" else "Container (LXC)"
            vm_role_color = "00ffff" if vm.type == "qemu" else "7fffd4"

            vm_role = await rest_reconcile_async(
                nb,
                "/api/dcim/device-roles/",
                lookup={"slug": vm_role_slug},
                payload={
                    "name": vm_role_name,
                    "slug": vm_role_slug,
                    "color": vm_role_color,
                    "description": f"Proxmox {'Virtual Machine' if vm.type == 'qemu' else 'LXC Container'}",
                    "tags": tag_refs,
                },
                schema=NetBoxDeviceRoleSyncState,
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "slug": record.get("slug"),
                    "color": record.get("color"),
                    "description": record.get("description"),
                    "tags": record.get("tags"),
                },
            )

            payload = build_netbox_virtual_machine_payload(
                proxmox_resource=vm.to_resource(),
                proxmox_config=vm.to_config(),
                cluster_id=cluster_obj.id,
                device_id=device.id,
                role_id=vm_role.id,
                tag_ids=[e2e_tag["id"]],
            )

            virtual_machine = await rest_reconcile_async(
                nb,
                "/api/virtualization/virtual-machines/",
                lookup={"name": vm.name},
                payload=payload,
                schema=NetBoxVirtualMachineCreateBody,
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "status": record.get("status"),
                    "cluster": record.get("cluster"),
                    "device": record.get("device"),
                    "role": record.get("role"),
                    "vcpus": record.get("vcpus"),
                    "memory": record.get("memory"),
                    "disk": record.get("disk"),
                    "tags": record.get("tags"),
                    "custom_fields": record.get("custom_fields"),
                    "description": record.get("description"),
                },
            )

            vm_backups = [b for b in all_backups if b.vmid == vm.vmid]
            created_backups = []

            for backup in vm_backups:
                storage = storage_lookup[backup.storage]
                try:
                    netbox_backup = await rest_reconcile_async(
                        nb,
                        "/api/plugins/proxbox/backups/",
                        lookup={"volume_id": backup.volid},
                        payload={
                            "storage": backup.storage,
                            "virtual_machine": virtual_machine.id,
                            "subtype": vm.type,
                            "creation_time": None,
                            "size": backup.size,
                            "verification_state": None,
                            "verification_upid": None,
                            "volume_id": backup.volid,
                            "notes": backup.notes,
                            "vmid": backup.vmid,
                            "format": backup.format,
                            "tags": tag_refs,
                        },
                        schema=NetBoxBackupSyncState,
                        current_normalizer=lambda record: {
                            "storage": record.get("storage"),
                            "virtual_machine": record.get("virtual_machine"),
                            "subtype": record.get("subtype"),
                            "creation_time": record.get("creation_time"),
                            "size": record.get("size"),
                            "verification_state": record.get("verification_state"),
                            "verification_upid": record.get("verification_upid"),
                            "volume_id": record.get("volume_id"),
                            "notes": record.get("notes"),
                            "vmid": record.get("vmid"),
                            "format": record.get("format"),
                            "tags": record.get("tags"),
                        },
                    )
                except ProxboxException as error:
                    raise AssertionError(
                        f"backup create failed for {backup.volid}: {error.detail}"
                    ) from error
                created_backups.append(netbox_backup)

            return created_backups

        all_vm_backups = await asyncio.gather(*[_create_vm_with_backups(vm) for vm in cluster.vms])

        total_backups = sum(len(backups) for backups in all_vm_backups)
        assert total_backups == len(all_backups), (
            f"Expected {len(all_backups)} backups, got {total_backups}"
        )

        for vm_backups in all_vm_backups:
            for backup in vm_backups:
                backup_data = backup.serialize()
                tag_slugs = [t.get("slug") for t in backup_data.get("tags", [])]
                assert "proxbox-e2e-testing" in tag_slugs, f"Backup {backup.id} missing e2e tag"

    async def _setup_cluster_dependencies(self, nb, cluster, tag_refs: list[dict[str, Any]]):
        """Set up cluster dependencies."""
        await self._get_cluster_type(nb, cluster.mode, tag_refs)

        manufacturer = await rest_reconcile_async(
            nb,
            "/api/dcim/manufacturers/",
            lookup={"slug": "proxmox"},
            payload={
                "name": "Proxmox",
                "slug": "proxmox",
                "tags": tag_refs,
            },
            schema=NetBoxManufacturerSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "slug": record.get("slug"),
                "tags": record.get("tags"),
            },
        )

        await rest_reconcile_async(
            nb,
            "/api/dcim/device-types/",
            lookup={"model": "Proxmox Generic Device"},
            payload={
                "model": "Proxmox Generic Device",
                "slug": "proxmox-generic-device",
                "manufacturer": manufacturer.id,
                "tags": tag_refs,
            },
            schema=NetBoxDeviceTypeSyncState,
            current_normalizer=lambda record: {
                "model": record.get("model"),
                "slug": record.get("slug"),
                "manufacturer": record.get("manufacturer"),
                "tags": record.get("tags"),
            },
        )

        await rest_reconcile_async(
            nb,
            "/api/dcim/device-roles/",
            lookup={"slug": "proxmox-node"},
            payload={
                "name": "Proxmox Node",
                "slug": "proxmox-node",
                "color": "00bcd4",
                "tags": tag_refs,
            },
            schema=NetBoxDeviceRoleSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "slug": record.get("slug"),
                "color": record.get("color"),
                "tags": record.get("tags"),
            },
        )

        site_name = f"Proxmox Default Site - {cluster.name}"
        site_slug = f"proxmox-default-site-{_slugify(cluster.name)}"
        await rest_reconcile_async(
            nb,
            "/api/dcim/sites/",
            lookup={"slug": site_slug},
            payload={
                "name": site_name,
                "slug": site_slug,
                "status": "active",
                "tags": tag_refs,
            },
            schema=NetBoxSiteSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "slug": record.get("slug"),
                "status": record.get("status"),
                "tags": record.get("tags"),
            },
        )

    async def _setup_cluster_storages(
        self, nb, cluster, cluster_obj, tag_refs: list[dict[str, Any]]
    ):
        """Create NetBox storage records for the cluster's backup stores."""
        storage_lookup = {}
        cluster_id = getattr(cluster_obj, "id", None) or cluster_obj.get("id")
        for storage in cluster.storage:
            storage_name = storage.get("storage")
            if not storage_name:
                continue
            storage_record = await rest_reconcile_async(
                nb,
                "/api/plugins/proxbox/storage/",
                lookup={"cluster": cluster_id, "name": storage_name},
                payload={
                    "cluster": cluster_id,
                    "name": storage_name,
                    "storage_type": storage.get("type"),
                    "content": storage.get("content"),
                    "path": storage.get("path"),
                    "nodes": storage.get("nodes"),
                    "shared": bool(storage.get("shared")),
                    "enabled": not bool(storage.get("disable")),
                    "tags": tag_refs,
                },
                schema=NetBoxStorageSyncState,
                current_normalizer=lambda record: {
                    "cluster": record.get("cluster", {}).get("id")
                    if isinstance(record.get("cluster"), dict)
                    else record.get("cluster"),
                    "name": record.get("name"),
                    "storage_type": record.get("storage_type"),
                    "content": record.get("content"),
                    "path": record.get("path"),
                    "nodes": record.get("nodes"),
                    "shared": record.get("shared"),
                    "enabled": record.get("enabled"),
                    "backups": record.get("backups"),
                    "tags": record.get("tags"),
                },
            )
            storage_lookup[storage_name] = storage_record
        return storage_lookup

    async def _get_cluster_type(self, nb, mode: str, tag_refs: list[dict[str, Any]]):
        """Get or create cluster type."""
        return await rest_reconcile_async(
            nb,
            "/api/virtualization/cluster-types/",
            lookup={"slug": mode},
            payload={
                "name": mode.capitalize(),
                "slug": mode,
                "description": f"Proxmox {mode} mode",
                "tags": tag_refs,
            },
            schema=NetBoxClusterTypeSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "slug": record.get("slug"),
                "description": record.get("description"),
                "tags": record.get("tags"),
            },
        )

    async def _get_or_create_device(
        self, nb, cluster, cluster_obj, node_name: str, tag_refs: list[dict[str, Any]]
    ):
        """Get or create a device for the node."""
        site_name = f"Proxmox Default Site - {cluster.name}"
        site_slug = f"proxmox-default-site-{_slugify(cluster.name)}"

        manufacturer = await rest_reconcile_async(
            nb,
            "/api/dcim/manufacturers/",
            lookup={"slug": "proxmox"},
            payload={
                "name": "Proxmox",
                "slug": "proxmox",
                "tags": tag_refs,
            },
            schema=NetBoxManufacturerSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "slug": record.get("slug"),
                "tags": record.get("tags"),
            },
        )

        device_type = await rest_reconcile_async(
            nb,
            "/api/dcim/device-types/",
            lookup={"model": "Proxmox Generic Device"},
            payload={
                "model": "Proxmox Generic Device",
                "slug": "proxmox-generic-device",
                "manufacturer": manufacturer.id,
                "tags": tag_refs,
            },
            schema=NetBoxDeviceTypeSyncState,
            current_normalizer=lambda record: {
                "model": record.get("model"),
                "slug": record.get("slug"),
                "manufacturer": record.get("manufacturer"),
                "tags": record.get("tags"),
            },
        )

        device_role = await rest_reconcile_async(
            nb,
            "/api/dcim/device-roles/",
            lookup={"slug": "proxmox-node"},
            payload={
                "name": "Proxmox Node",
                "slug": "proxmox-node",
                "color": "00bcd4",
                "tags": tag_refs,
            },
            schema=NetBoxDeviceRoleSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "slug": record.get("slug"),
                "color": record.get("color"),
                "tags": record.get("tags"),
            },
        )

        site = await rest_reconcile_async(
            nb,
            "/api/dcim/sites/",
            lookup={"slug": site_slug},
            payload={
                "name": site_name,
                "slug": site_slug,
                "status": "active",
                "tags": tag_refs,
            },
            schema=NetBoxSiteSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "slug": record.get("slug"),
                "status": record.get("status"),
                "tags": record.get("tags"),
            },
        )

        return await rest_reconcile_async(
            nb,
            "/api/dcim/devices/",
            lookup={"name": node_name, "site_id": site.id},
            payload={
                "name": node_name,
                "tags": tag_refs,
                "cluster": cluster_obj.id,
                "status": "active",
                "description": f"Proxmox Node {node_name}",
                "device_type": device_type.id,
                "role": device_role.id,
                "site": site.id,
            },
            schema=NetBoxDeviceSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "status": record.get("status"),
                "cluster": record.get("cluster"),
                "device_type": record.get("device_type"),
                "role": record.get("role"),
                "site": record.get("site"),
                "description": record.get("description"),
                "tags": record.get("tags"),
            },
        )
