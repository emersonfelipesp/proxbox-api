"""Base class for individual sync services."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from proxbox_api.netbox_rest import nested_tag_payload

if TYPE_CHECKING:
    pass


class BaseIndividualSyncService:
    """Base class for individual sync services.

    Provides common functionality for syncing a single object from Proxmox to NetBox.
    Each subclass should override `sync_one` to implement object-specific logic.
    """

    def __init__(
        self,
        nb: object,
        px: object,
        tag: object,
    ) -> None:
        """Initialize the sync service.

        Args:
            nb: NetBox async session.
            px: Single Proxmox session (resolved by cluster_name).
            tag: ProxboxTagDep object.
        """
        self.nb = nb
        self.px = px
        self.tag = tag
        self.tag_refs = self._build_tag_refs(tag)

    def _build_tag_refs(self, tag: object) -> list[dict[str, object]]:
        """Build tag refs list from tag object.

        Args:
            tag: Tag object with name, slug, color attributes.

        Returns:
            List containing tag payload dict.
        """
        return nested_tag_payload(tag)

    def _last_updated_cf(self) -> dict[str, str]:
        """Return proxmox_last_updated custom field with current timestamp.

        Returns:
            Dict with proxmox_last_updated key and ISO timestamp value.
        """
        return {"proxmox_last_updated": datetime.now(timezone.utc).isoformat()}

    async def sync_one(self, **params: object) -> dict:
        """Sync a single object. Override in subclasses.

        Args:
            **params: Sync-specific parameters.

        Returns:
            IndividualSyncResponse dict.

        Raises:
            NotImplementedError: When subclass doesn't override.
        """
        msg = "sync_one must be implemented by subclass"
        raise NotImplementedError(msg)

    async def _get_or_create_cluster(
        self,
        cluster_name: str,
        mode: str = "cluster",
    ) -> object:
        """Get or create a cluster with its type.

        Args:
            cluster_name: Name of the cluster.
            mode: Cluster mode (e.g., 'cluster', 'standalone').

        Returns:
            NetBox cluster object.
        """
        from proxbox_api.services.sync.device_ensure import (
            _ensure_cluster,
            _ensure_cluster_type,
        )

        cluster_type = await _ensure_cluster_type(
            self.nb,
            mode=mode,
            tag_refs=self.tag_refs,
        )
        cluster = await _ensure_cluster(
            self.nb,
            cluster_name=cluster_name,
            cluster_type_id=getattr(cluster_type, "id", None),
            mode=mode,
            tag_refs=self.tag_refs,
        )
        return cluster

    async def _get_or_create_manufacturer(self) -> object:
        """Get or create the Proxmox manufacturer.

        Returns:
            NetBox manufacturer object.
        """
        from proxbox_api.services.sync.device_ensure import _ensure_manufacturer

        return await _ensure_manufacturer(self.nb, tag_refs=self.tag_refs)

    async def _get_or_create_device_type(self, manufacturer_id: int | None) -> object:
        """Get or create the Proxmox Generic Device device type.

        Args:
            manufacturer_id: NetBox manufacturer ID.

        Returns:
            NetBox device type object.
        """
        from proxbox_api.services.sync.device_ensure import _ensure_device_type

        return await _ensure_device_type(
            self.nb,
            manufacturer_id=manufacturer_id,
            tag_refs=self.tag_refs,
        )

    async def _get_or_create_device_role_node(self) -> object:
        """Get or create the Proxmox Node device role.

        Returns:
            NetBox device role object.
        """
        from proxbox_api.services.sync.device_ensure import _ensure_device_role

        return await _ensure_device_role(self.nb, tag_refs=self.tag_refs)

    async def _get_or_create_vm_role(self, vm_type: str) -> object:
        """Get or create the VM/Container device role based on type.

        Args:
            vm_type: 'qemu' or 'lxc'.

        Returns:
            NetBox device role object.
        """
        from proxbox_api.netbox_rest import rest_reconcile_async
        from proxbox_api.proxmox_to_netbox.models import NetBoxDeviceRoleSyncState

        role_mapping = {
            "qemu": {
                "name": "Virtual Machine (QEMU)",
                "slug": "virtual-machine-qemu",
                "color": "00ffff",
                "description": "Proxmox Virtual Machine",
            },
            "lxc": {
                "name": "Container (LXC)",
                "slug": "container-lxc",
                "color": "7fffd4",
                "description": "Proxmox LXC Container",
            },
        }
        role_data = role_mapping.get(vm_type, role_mapping["qemu"])

        return await rest_reconcile_async(
            self.nb,
            "/api/dcim/device-roles/",
            lookup={"slug": role_data["slug"]},
            payload={
                **role_data,
                "tags": self.tag_refs,
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

    async def _get_or_create_site(self, cluster_name: str) -> object:
        """Get or create the site for a cluster.

        Args:
            cluster_name: Name of the cluster.

        Returns:
            NetBox site object.
        """
        from proxbox_api.services.sync.device_ensure import _ensure_site

        return await _ensure_site(
            self.nb,
            cluster_name=cluster_name,
            tag_refs=self.tag_refs,
        )

    async def _get_or_create_device(
        self,
        device_name: str,
        cluster_id: int | None,
        device_type_id: int | None,
        role_id: int | None,
        site_id: int | None,
    ) -> object:
        """Get or create a device (node) in NetBox.

        Args:
            device_name: Name of the device.
            cluster_id: NetBox cluster ID.
            device_type_id: NetBox device type ID.
            role_id: NetBox device role ID.
            site_id: NetBox site ID.

        Returns:
            NetBox device object.
        """
        from proxbox_api.services.sync.device_ensure import _ensure_device

        return await _ensure_device(
            self.nb,
            device_name=device_name,
            cluster_id=cluster_id,
            device_type_id=device_type_id,
            role_id=role_id,
            site_id=site_id,
            tag_refs=self.tag_refs,
        )

    async def _get_or_create_vm_dependencies(
        self,
        cluster_name: str,
        node_name: str,
        vm_type: str,
    ) -> tuple[object, object, object, object, object, object, object, object]:
        """Get or create all VM dependencies.

        Args:
            cluster_name: Cluster name.
            node_name: Node (host) name.
            vm_type: 'qemu' or 'lxc'.

        Returns:
            Tuple of (cluster, cluster_type, manufacturer, device_type, node_role, site, device, vm_role).
        """
        from proxbox_api.services.sync.device_ensure import (
            _ensure_cluster_type,
        )

        cluster_mode = "cluster"
        cluster_type = await _ensure_cluster_type(
            self.nb,
            mode=cluster_mode,
            tag_refs=self.tag_refs,
        )
        cluster = await self._get_or_create_cluster(cluster_name, cluster_mode)
        manufacturer = await self._get_or_create_manufacturer()
        device_type = await self._get_or_create_device_type(getattr(manufacturer, "id", None))
        node_role = await self._get_or_create_device_role_node()
        site = await self._get_or_create_site(cluster_name)
        device = await self._get_or_create_device(
            device_name=node_name,
            cluster_id=getattr(cluster, "id", None),
            device_type_id=getattr(device_type, "id", None),
            role_id=getattr(node_role, "id", None),
            site_id=getattr(site, "id", None),
        )
        vm_role = await self._get_or_create_vm_role(vm_type)

        return cluster, cluster_type, manufacturer, device_type, node_role, site, device, vm_role

    def _build_response(
        self,
        object_type: str,
        action: str,
        proxmox_resource: dict | None = None,
        netbox_object: dict | None = None,
        dry_run: bool = False,
        dependencies_synced: list[dict] | None = None,
        error: str | None = None,
    ) -> dict:
        """Build a standardized sync response dict.

        Args:
            object_type: Type of object (e.g., 'vm', 'interface').
            action: Action taken ('created', 'updated', 'noop', 'dry_run').
            proxmox_resource: Data fetched from Proxmox.
            netbox_object: Data created/updated in NetBox.
            dry_run: Whether this was a dry run.
            dependencies_synced: List of dependencies that were synced.
            error: Error message if any.

        Returns:
            Standardized response dict.
        """
        return {
            "object_type": object_type,
            "action": action,
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": dry_run,
            "dependencies_synced": dependencies_synced or [],
            "error": error,
        }
