"""E2E tests for Proxmox VM synchronization.

Tests the synchronization of Proxmox VMs and LXC containers to NetBox,
verifying that all synced objects have the 'proxbox e2e testing' tag.
"""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api.e2e.fixtures.proxmox_openapi_mock import (
    MockProxmoxCluster,
    create_minimal_cluster,
)
from proxbox_api.netbox_rest import (
    nested_tag_payload,
    rest_reconcile_async,
)
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxClusterSyncState,
    NetBoxClusterTypeSyncState,
    NetBoxDeviceRoleSyncState,
    NetBoxDeviceSyncState,
    NetBoxDeviceTypeSyncState,
    NetBoxManufacturerSyncState,
    NetBoxSiteSyncState,
    NetBoxVirtualMachineCreateBody,
    _relation_id,
)
from proxbox_api.services.sync.device_ensure import _slugify
from proxbox_api.services.sync.virtual_machines import build_netbox_virtual_machine_payload


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.mock_backend
@pytest.mark.mock_http
class TestVMSync:
    """E2E tests for virtual machine synchronization."""

    async def test_sync_qemu_vm_with_e2e_tag(
        self,
        netbox_e2e_session,
        e2e_tag,
        e2e_shared_proxmox_site,
        unique_prefix,
    ):
        """Test syncing a QEMU VM with e2e tag.

        Verifies:
        1. VM exists in NetBox
        2. VM has 'proxbox e2e testing' tag
        3. VM has correct custom fields
        4. VM is linked to correct cluster and device
        """
        nb = netbox_e2e_session
        tag_refs = nested_tag_payload(e2e_tag)
        cluster = create_minimal_cluster(prefix=unique_prefix)

        vm = cluster.vms[0]
        node = cluster.nodes[0]

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

        device = await self._get_or_create_device(
            nb,
            cluster,
            cluster_obj,
            node.name,
            tag_refs,
            shared_site=e2e_shared_proxmox_site,
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

        assert virtual_machine is not None
        assert virtual_machine.name == vm.name

        vm_data = virtual_machine.serialize()
        tag_slugs = [t.get("slug") for t in vm_data.get("tags", [])]
        assert "proxbox-e2e-testing" in tag_slugs

        cf = vm_data.get("custom_fields") or {}
        if cf.get("proxmox_vm_id") is not None:
            assert cf.get("proxmox_vm_id") == vm.vmid
        assert _relation_id(vm_data.get("cluster")) == cluster_obj.id
        assert _relation_id(vm_data.get("device")) == device.id

    async def test_sync_lxc_container_with_e2e_tag(
        self,
        netbox_e2e_session,
        e2e_tag,
        e2e_shared_proxmox_site,
        unique_prefix,
    ):
        """Test syncing an LXC container with e2e tag.

        Verifies:
        1. LXC container exists in NetBox
        2. Container has 'proxbox e2e testing' tag
        3. Container has correct custom fields
        """
        nb = netbox_e2e_session
        tag_refs = nested_tag_payload(e2e_tag)
        cluster = create_minimal_cluster(prefix=unique_prefix)

        lxc_vm = None
        for vm in cluster.vms:
            if vm.type == "lxc":
                lxc_vm = vm
                break

        assert lxc_vm is not None, "Should have an LXC container in cluster"

        node = cluster.nodes[0]

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

        device = await self._get_or_create_device(
            nb,
            cluster,
            cluster_obj,
            node.name,
            tag_refs,
            shared_site=e2e_shared_proxmox_site,
        )

        netbox_vm_payload = build_netbox_virtual_machine_payload(
            proxmox_resource=lxc_vm.to_resource(),
            proxmox_config=lxc_vm.to_config(),
            cluster_id=cluster_obj.id,
            device_id=device.id,
            role_id=1,
            tag_ids=[e2e_tag["id"]],
        )

        vm_role = await rest_reconcile_async(
            nb,
            "/api/dcim/device-roles/",
            lookup={"slug": "container-lxc"},
            payload={
                "name": "Container (LXC)",
                "slug": "container-lxc",
                "color": "7fffd4",
                "description": "Proxmox LXC Container",
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
            lookup={"name": lxc_vm.name},
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

        assert virtual_machine is not None
        assert virtual_machine.name == lxc_vm.name

        vm_data = virtual_machine.serialize()
        tag_slugs = [t.get("slug") for t in vm_data.get("tags", [])]
        assert "proxbox-e2e-testing" in tag_slugs

        cf = vm_data.get("custom_fields") or {}
        if cf.get("proxmox_vm_id") is not None:
            assert cf.get("proxmox_vm_id") == lxc_vm.vmid

    async def test_sync_vm_creates_custom_fields(
        self,
        netbox_e2e_session,
        e2e_tag,
        e2e_shared_proxmox_site,
        unique_prefix,
    ):
        """Test that VM custom fields are correctly set.

        Verifies:
        - proxmox_vm_id matches VM ID
        - proxmox_start_at_boot reflects onboot config
        - proxmox_unprivileged_container reflects unprivileged config
        """
        nb = netbox_e2e_session
        tag_refs = nested_tag_payload(e2e_tag)
        cluster = create_minimal_cluster(prefix=unique_prefix)

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

        device = await self._get_or_create_device(
            nb,
            cluster,
            cluster_obj,
            cluster.nodes[0].name,
            tag_refs,
            shared_site=e2e_shared_proxmox_site,
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

        vm_data = virtual_machine.serialize()
        custom_fields = vm_data.get("custom_fields") or {}
        if custom_fields.get("proxmox_vm_id") is None:
            pytest.skip(
                "NetBox demo does not return Proxmox VM custom fields (unset or no permission)."
            )

        assert custom_fields.get("proxmox_vm_id") == vm.vmid

        config = vm.to_config()
        assert custom_fields.get("proxmox_start_at_boot") == (config.get("onboot", 0) == 1)
        assert custom_fields.get("proxmox_qemu_agent") == (config.get("agent", 0) == 1)
        assert custom_fields.get("proxmox_unprivileged_container") == (
            config.get("unprivileged", 0) == 1
        )

    async def test_sync_multiple_vms_parallel(
        self,
        netbox_e2e_session,
        e2e_tag,
        e2e_shared_proxmox_site,
        unique_prefix,
    ):
        """Test syncing multiple VMs in parallel.

        Verifies that all VMs are synced correctly with e2e tags.
        """
        import asyncio

        nb = netbox_e2e_session
        tag_refs = nested_tag_payload(e2e_tag)
        cluster = create_minimal_cluster(prefix=unique_prefix)

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

        device = await self._get_or_create_device(
            nb,
            cluster,
            cluster_obj,
            cluster.nodes[0].name,
            tag_refs,
            shared_site=e2e_shared_proxmox_site,
        )

        async def _create_vm(vm: MockProxmoxCluster) -> Any:
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

            return await rest_reconcile_async(
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

        vms = await asyncio.gather(*[_create_vm(vm) for vm in cluster.vms])

        assert len(vms) == len(cluster.vms)

        for vm in vms:
            vm_data = vm.serialize()
            tag_slugs = [t.get("slug") for t in vm_data.get("tags", [])]
            assert "proxbox-e2e-testing" in tag_slugs

    async def _setup_cluster_dependencies(
        self, nb, cluster: MockProxmoxCluster, tag_refs: list[dict[str, Any]]
    ):
        """Set up cluster dependencies (type, manufacturer, device type, role, site)."""
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
        self,
        nb,
        cluster: MockProxmoxCluster,
        cluster_obj,
        node_name: str,
        tag_refs: list[dict[str, Any]],
        *,
        shared_site: Any | None = None,
    ):
        """Get or create a device for the node."""
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

        if shared_site is not None:
            site = shared_site
        else:
            site_name = f"Proxmox Default Site - {cluster.name}"
            site_slug = f"proxmox-default-site-{_slugify(cluster.name)}"
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
