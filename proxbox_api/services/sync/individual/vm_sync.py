"""Individual Virtual Machine sync service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from proxbox_api.enum.status_mapping import ProxmoxToNetBoxVMStatus
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxVirtualMachineCreateBody,
)
from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.proxmox_helpers import (
    get_vm_config_individual,
    get_vm_resource_individual,
)
from proxbox_api.services.sync.individual.base import BaseIndividualSyncService
from proxbox_api.services.sync.individual.interface_sync import sync_interface_individual
from proxbox_api.services.sync.vm_helpers import _compute_vm_patchable_fields


def _mb_from_bytes(value: object) -> int:
    """Convert bytes to megabytes."""
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        return 0
    if as_int <= 0:
        return 0
    return as_int // (1024 * 1024)


def _status_value(value: object) -> str:
    """Normalize Proxmox VM status to NetBox VM status."""
    return ProxmoxToNetBoxVMStatus.from_proxmox(value or "active")


def _as_bool(value: object) -> bool:
    """Convert value to boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _build_netbox_vm_payload(
    resource: dict,
    config: dict,
    cluster_id: int,
    device_id: int | None,
    role_id: int | None,
    tag_ids: list[int],
    last_updated: datetime,
    cluster_name: str | None = None,
    proxmox_url: str | None = None,
    virtual_machine_type_id: int | None = None,
) -> dict:
    """Build NetBox VM payload from Proxmox resource and config."""
    vm_type = str(resource.get("type", "qemu")).lower()
    if vm_type not in ("qemu", "lxc"):
        vm_type = "qemu"

    onboot = config.get("onboot", 0) if config else 0
    agent = config.get("agent", 0) if config else 0
    unprivileged = config.get("unprivileged", 0) if config else 0
    searchdomain = config.get("searchdomain", None) if config else None

    maxcpu = int(resource.get("maxcpu") or 0)
    maxmem = resource.get("maxmem")
    maxdisk = resource.get("maxdisk")

    memory_mb = _mb_from_bytes(maxmem)
    disk_mb = _mb_from_bytes(maxdisk)

    status = _status_value(resource.get("status", "stopped"))
    node = resource.get("node", "unknown")
    proxmox_status = str(resource.get("status", "stopped"))

    vm_custom_fields: dict[str, object] = {
        "proxmox_vm_id": int(resource.get("vmid") or 0),
        "proxmox_vm_type": vm_type,
        "proxmox_start_at_boot": _as_bool(onboot),
        "proxmox_unprivileged_container": _as_bool(unprivileged),
        "proxmox_qemu_agent": _as_bool(agent),
        "proxmox_search_domain": searchdomain,
        "proxmox_node": node,
        "proxmox_status": proxmox_status,
        "proxmox_last_updated": last_updated.isoformat(),
    }
    if cluster_name:
        vm_custom_fields["proxmox_cluster"] = cluster_name
    if proxmox_url:
        vmid = int(resource.get("vmid") or 0)
        vm_custom_fields["proxmox_link"] = f"{proxmox_url}/#v1:0:={vm_type}/{vmid}"

    payload: dict[str, object] = {
        "name": str(resource.get("name", "")),
        "status": status,
        "cluster": cluster_id,
        "device": device_id,
        "role": role_id,
        "vcpus": maxcpu,
        "memory": memory_mb,
        "disk": disk_mb,
        "tags": tag_ids,
        "custom_fields": vm_custom_fields,
        "description": f"Synced from Proxmox node {node}",
    }
    if virtual_machine_type_id is not None:
        payload["virtual_machine_type"] = virtual_machine_type_id
    return payload


async def sync_vm_individual(
    nb: object,
    px: object,
    tag: object,
    cluster_name: str,
    node: str,
    vm_type: str,
    vmid: int,
    dry_run: bool = False,
    overwrite_flags: SyncOverwriteFlags | None = None,
) -> dict:
    """Sync a single Virtual Machine from Proxmox to NetBox.

    Auto-creates cluster, device (node), and VM role if they don't exist.

    Args:
        nb: NetBox async session.
        px: Single Proxmox session.
        tag: ProxboxTagDep object.
        cluster_name: Name of the cluster.
        node: Proxmox node name.
        vm_type: 'qemu' or 'lxc'.
        vmid: Proxmox VM ID.
        dry_run: If True, return what would be synced without making changes.
        overwrite_flags: Per-field overwrite gates for existing VM updates.

    Returns:
        IndividualSyncResponse dict.
    """
    service = BaseIndividualSyncService(nb, px, tag, overwrite_flags=overwrite_flags)
    now = datetime.now(timezone.utc)

    tag_id = int(getattr(tag, "id", 0) or 0)
    tag_ids = [tag_id] if tag_id > 0 else []

    try:
        proxmox_config = await get_vm_config_individual(px, node, vm_type, vmid)
    except Exception as exc:
        logger.warning(
            "Failed to fetch Proxmox config for %s/%s vmid=%s: %s",
            node,
            vm_type,
            vmid,
            exc,
        )
        raise ProxboxException(
            message=(
                f"Failed to fetch Proxmox config for {vm_type} vmid={vmid} on node {node}; "
                "refusing to sync with empty config to avoid overwriting NetBox values."
            ),
            python_exception=str(exc),
        )

    proxmox_resource = get_vm_resource_individual(px, node, vm_type, vmid) or {}
    proxmox_resource = {
        "vmid": vmid,
        "name": proxmox_resource.get("name") or f"vm-{vmid}",
        "node": proxmox_resource.get("node") or node,
        "type": proxmox_resource.get("type") or vm_type,
        "status": proxmox_resource.get("status") or "unknown",
        "maxcpu": proxmox_resource.get("maxcpu") or 0,
        "maxmem": proxmox_resource.get("maxmem") or 0,
        "maxdisk": proxmox_resource.get("maxdisk") or 0,
        "config": proxmox_config,
        "proxmox_last_updated": now.isoformat(),
    }

    if dry_run:
        existing = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"cf_proxmox_vm_id": vmid},
        )
        netbox_object = None
        if existing:
            netbox_object = existing[0].serialize() if hasattr(existing[0], "serialize") else None

        cluster_dep: dict[str, object] = {
            "object_type": "cluster",
            "name": cluster_name,
            "cluster_name": cluster_name,
        }
        node_dep: dict[str, object] = {
            "object_type": "node",
            "name": node,
            "cluster_name": cluster_name,
        }
        return {
            "object_type": "vm",
            "action": "dry_run",
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": True,
            "dependencies_synced": [cluster_dep, node_dep],
            "error": None,
        }

    try:
        (
            cluster,
            _cluster_type,
            _manufacturer,
            _device_type,
            _node_role,
            _site,
            device,
            vm_role,
            vm_type_obj,
        ) = await service._get_or_create_vm_dependencies(cluster_name, node, vm_type)

        cluster_id = int(getattr(cluster, "id", 0) or 0)
        device_id = int(getattr(device, "id", 0) or 0) if device else None
        role_id = int(getattr(vm_role, "id", 0) or 0) if vm_role else None
        vm_type_id = int(getattr(vm_type_obj, "id", 0) or 0) if vm_type_obj else None

        px_domain = getattr(px, "domain", None) or getattr(px, "ip_address", None) or ""
        px_port = getattr(px, "http_port", 8006)
        px_url = f"https://{px_domain}:{px_port}" if px_domain else None

        existing_vms = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"cf_proxmox_vm_id": vmid, "cluster_id": cluster_id},
        )

        # Merge proxbox tag with any existing user tags so sync never erases them.
        existing_tag_ids: list[int] = []
        if existing_vms:
            raw_record = existing_vms[0]
            raw_tags = (
                raw_record.get("tags")
                if isinstance(raw_record, dict)
                else getattr(raw_record, "tags", None)
            ) or []
            for t in raw_tags:
                tid = t.get("id") if isinstance(t, dict) else t
                try:
                    existing_tag_ids.append(int(tid))
                except (TypeError, ValueError):
                    continue
        merged_tag_ids = sorted(set(tag_ids) | set(existing_tag_ids))

        netbox_vm_payload = _build_netbox_vm_payload(
            resource=proxmox_resource,
            config=proxmox_config,
            cluster_id=cluster_id,
            device_id=device_id,
            role_id=role_id,
            tag_ids=merged_tag_ids,
            last_updated=now,
            cluster_name=cluster_name,
            proxmox_url=px_url,
            virtual_machine_type_id=vm_type_id,
        )

        virtual_machine = await rest_reconcile_async(
            nb,
            "/api/virtualization/virtual-machines/",
            lookup={
                "cf_proxmox_vm_id": vmid,
                "cluster_id": cluster_id,
            },
            payload=netbox_vm_payload,
            schema=NetBoxVirtualMachineCreateBody,
            patchable_fields=frozenset(_compute_vm_patchable_fields(overwrite_flags)),
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "status": record.get("status"),
                "cluster": record.get("cluster"),
                "device": record.get("device"),
                "virtual_machine_type": record.get("virtual_machine_type"),
                "role": record.get("role"),
                "vcpus": record.get("vcpus"),
                "memory": record.get("memory"),
                "disk": record.get("disk"),
                "tags": record.get("tags"),
                "custom_fields": record.get("custom_fields"),
                "description": record.get("description"),
            },
        )

        netbox_object = (
            virtual_machine.serialize() if hasattr(virtual_machine, "serialize") else None
        )
        action = "updated" if existing_vms else "created"

        dependencies: list[dict] = [
            {
                "object_type": "cluster",
                "name": cluster_name,
                "cluster_name": cluster_name,
                "action": action,
            },
            {
                "object_type": "node",
                "name": node,
                "cluster_name": cluster_name,
                "action": action,
            },
        ]

        return {
            "object_type": "vm",
            "action": action,
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": False,
            "dependencies_synced": dependencies,
            "error": None,
        }

    except Exception as error:
        return {
            "object_type": "vm",
            "action": "error",
            "proxmox_resource": proxmox_resource,
            "netbox_object": None,
            "dry_run": False,
            "dependencies_synced": [],
            "error": str(error),
        }


async def sync_vm_with_related(
    nb: object,
    px: object,
    tag: object,
    cluster_name: str,
    node: str,
    vm_type: str,
    vmid: int,
    dry_run: bool = False,
    sync_interfaces: bool = True,
    sync_task_history: bool = True,
    overwrite_flags: SyncOverwriteFlags | None = None,
) -> dict:
    """Sync a VM with its related objects (interfaces, task history) in parallel.

    Args:
        nb: NetBox async session.
        px: Single Proxmox session.
        tag: ProxboxTagDep object.
        cluster_name: Name of the cluster.
        node: Proxmox node name.
        vm_type: 'qemu' or 'lxc'.
        vmid: Proxmox VM ID.
        dry_run: If True, don't make changes.
        sync_interfaces: Whether to sync interfaces.
        sync_task_history: Whether to sync task history.

    Returns:
        Dict with VM result and related sync results.
    """
    from proxbox_api.services.sync.individual.task_history_sync import sync_task_history_individual

    vm_result = await sync_vm_individual(
        nb,
        px,
        tag,
        cluster_name,
        node,
        vm_type,
        vmid,
        dry_run,
        overwrite_flags=overwrite_flags,
    )

    related_results: list[dict] = []
    related_dependencies: list[dict] = []

    tasks_to_gather: list[tuple[str, object]] = []

    if sync_interfaces:
        try:
            vm_config = await get_vm_config_individual(px, node, vm_type, vmid)
        except Exception:
            vm_config = {}
        interface_names = sorted(
            key
            for key in vm_config
            if isinstance(key, str) and key.startswith("net") and not key.startswith("nets")
        )
        if not interface_names and vm_type == "qemu":
            interface_names = ["net0"]
        for interface_name in interface_names:
            tasks_to_gather.append(
                (
                    "interface",
                    sync_interface_individual(
                        nb,
                        px,
                        tag,
                        node,
                        vm_type,
                        vmid,
                        interface_name,
                        auto_create_vm=False,
                        dry_run=dry_run,
                    ),
                )
            )

    if sync_task_history:
        tasks_to_gather.append(
            (
                "task_history",
                sync_task_history_individual(
                    nb,
                    px,
                    tag,
                    node,
                    vm_type,
                    vmid,
                    upid=None,
                    auto_create_vm=False,
                    cluster_name=cluster_name,
                    dry_run=dry_run,
                ),
            )
        )

    if tasks_to_gather:
        results = await asyncio.gather(
            *(coroutine for _kind, coroutine in tasks_to_gather),
            return_exceptions=True,
        )
        for (kind, _coroutine), result in zip(tasks_to_gather, results, strict=False):
            if isinstance(result, Exception):
                related_results.append({"object_type": kind, "error": str(result)})
            else:
                related_results.append(result)
                if isinstance(result, dict):
                    related_dependencies.extend(result.get("dependencies_synced", []))

    return {
        "vm": vm_result,
        "related": related_results,
        "dependencies_synced": related_dependencies,
    }
