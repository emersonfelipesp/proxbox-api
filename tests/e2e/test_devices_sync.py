"""E2E tests for Proxmox device (node) synchronization.

Tests the synchronization of Proxmox nodes to NetBox devices,
verifying that all synced objects have the 'proxbox e2e testing' tag.
"""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api.e2e.fixtures.proxmox_mock import (
    create_minimal_cluster,
    create_multi_cluster,
)
from proxbox_api.netbox_rest import (
    nested_tag_payload,
    rest_list_async,
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
)


@pytest.mark.asyncio
class TestDevicesSync:
    """E2E tests for device synchronization."""

    async def test_sync_single_node_creates_device_with_e2e_tag(
        self,
        netbox_demo_session,
        e2e_tag,
        unique_prefix,
    ):
        """Test that syncing a single node creates a device with e2e tag.

        This test verifies:
        1. A Proxmox node is synced to a NetBox device
        2. The device has the 'proxbox e2e testing' tag
        3. All dependent objects also have the e2e tag
        """
        nb = netbox_demo_session
        tag_refs = nested_tag_payload(e2e_tag)
        cluster = create_minimal_cluster(prefix=unique_prefix)

        node_name = cluster.nodes[0].name
        cluster_name = cluster.name

        cluster_type = await rest_reconcile_async(
            nb,
            "/api/virtualization/cluster-types/",
            lookup={"slug": cluster.mode},
            payload={
                "name": cluster.mode.capitalize(),
                "slug": cluster.mode,
                "description": f"Proxmox {cluster.mode} mode",
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

        assert cluster_type is not None
        cluster_obj = await rest_reconcile_async(
            nb,
            "/api/virtualization/clusters/",
            lookup={"name": cluster_name},
            payload={
                "name": cluster_name,
                "type": cluster_type.id,
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

        assert cluster_obj is not None

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

        from proxbox_api.services.sync.devices import _slugify

        site_name = f"Proxmox Default Site - {cluster_name}"
        site_slug = f"proxmox-default-site-{_slugify(cluster_name)}"
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

        device = await rest_reconcile_async(
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

        assert device is not None
        assert device.name == node_name
        assert device.cluster == cluster_obj.id
        assert device.site == site.id

        devices = await rest_list_async(
            nb,
            "/api/dcim/devices/",
            query={"name": node_name, "site_id": site.id},
        )
        assert len(devices) == 1

        device_data = devices[0].serialize()
        device_tag_slugs = [t.get("slug") for t in device_data.get("tags", [])]
        assert "proxbox-e2e-testing" in device_tag_slugs

    async def test_sync_creates_all_dependent_objects_with_e2e_tag(
        self,
        netbox_demo_session,
        e2e_tag,
        unique_prefix,
    ):
        """Test that all dependent objects are created with e2e tag.

        Verifies:
        - Cluster type has e2e tag
        - Cluster has e2e tag
        - Site has e2e tag
        - Manufacturer has e2e tag
        - Device type has e2e tag
        - Device role has e2e tag
        """
        nb = netbox_demo_session
        tag_refs = nested_tag_payload(e2e_tag)
        cluster = create_minimal_cluster(prefix=unique_prefix)

        cluster_type = await rest_reconcile_async(
            nb,
            "/api/virtualization/cluster-types/",
            lookup={"slug": cluster.mode},
            payload={
                "name": cluster.mode.capitalize(),
                "slug": cluster.mode,
                "description": f"Proxmox {cluster.mode} mode",
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

        cluster_obj = await rest_reconcile_async(
            nb,
            "/api/virtualization/clusters/",
            lookup={"name": cluster.name},
            payload={
                "name": cluster.name,
                "type": cluster_type.id,
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

        from proxbox_api.services.sync.devices import _slugify

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

        cluster_type_data = cluster_type.serialize()
        cluster_data = cluster_obj.serialize()
        manufacturer_data = manufacturer.serialize()
        device_type_data = device_type.serialize()
        device_role_data = device_role.serialize()
        site_data = site.serialize()

        def _has_e2e_tag(obj: dict[str, Any]) -> bool:
            tag_slugs = [t.get("slug") for t in obj.get("tags", [])]
            return "proxbox-e2e-testing" in tag_slugs

        assert _has_e2e_tag(cluster_type_data), "Cluster type missing e2e tag"
        assert _has_e2e_tag(cluster_data), "Cluster missing e2e tag"
        assert _has_e2e_tag(manufacturer_data), "Manufacturer missing e2e tag"
        assert _has_e2e_tag(device_type_data), "Device type missing e2e tag"
        assert _has_e2e_tag(device_role_data), "Device role missing e2e tag"
        assert _has_e2e_tag(site_data), "Site missing e2e tag"

    async def test_idempotent_sync_does_not_duplicate(
        self,
        netbox_demo_session,
        e2e_tag,
        unique_prefix,
    ):
        """Test that running sync twice doesn't create duplicates.

        Verifies that the sync is idempotent and updates existing
        objects instead of creating new ones.
        """
        nb = netbox_demo_session
        tag_refs = nested_tag_payload(e2e_tag)
        cluster = create_minimal_cluster(prefix=unique_prefix)

        node_name = cluster.nodes[0].name

        async def _create_device():
            cluster_type = await rest_reconcile_async(
                nb,
                "/api/virtualization/cluster-types/",
                lookup={"slug": cluster.mode},
                payload={
                    "name": cluster.mode.capitalize(),
                    "slug": cluster.mode,
                    "description": f"Proxmox {cluster.mode} mode",
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

            cluster_obj = await rest_reconcile_async(
                nb,
                "/api/virtualization/clusters/",
                lookup={"name": cluster.name},
                payload={
                    "name": cluster.name,
                    "type": cluster_type.id,
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

            from proxbox_api.services.sync.devices import _slugify

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

        device1 = await _create_device()
        device1_id = device1.id

        device2 = await _create_device()
        device2_id = device2.id

        assert device1_id == device2_id, "Idempotent sync should not create duplicates"

        devices = await rest_list_async(
            nb,
            "/api/dcim/devices/",
            query={"name": node_name},
        )
        assert len(devices) == 1, "Should have exactly one device with that name"

    async def test_sync_with_multiple_nodes(
        self,
        netbox_demo_session,
        e2e_tag,
        unique_prefix,
    ):
        """Test syncing multiple nodes in parallel.

        Verifies that the sync handles multiple nodes correctly
        and all nodes get the e2e tag.
        """
        import asyncio

        nb = netbox_demo_session
        tag_refs = nested_tag_payload(e2e_tag)
        clusters = create_multi_cluster(prefix=unique_prefix)

        cluster = clusters[0]
        cluster_name = cluster.name
        node_names = [node.name for node in cluster.nodes]

        cluster_type = await rest_reconcile_async(
            nb,
            "/api/virtualization/cluster-types/",
            lookup={"slug": cluster.mode},
            payload={
                "name": cluster.mode.capitalize(),
                "slug": cluster.mode,
                "description": f"Proxmox {cluster.mode} mode",
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

        cluster_obj = await rest_reconcile_async(
            nb,
            "/api/virtualization/clusters/",
            lookup={"name": cluster_name},
            payload={
                "name": cluster_name,
                "type": cluster_type.id,
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

        from proxbox_api.services.sync.devices import _slugify

        site_name = f"Proxmox Default Site - {cluster_name}"
        site_slug = f"proxmox-default-site-{_slugify(cluster_name)}"
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

        async def _create_node(node_name: str):
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

        devices = await asyncio.gather(*[_create_node(name) for name in node_names])

        assert len(devices) == len(node_names)

        for device in devices:
            assert device.name in node_names
            device_data = device.serialize()
            tag_slugs = [t.get("slug") for t in device_data.get("tags", [])]
            assert "proxbox-e2e-testing" in tag_slugs
