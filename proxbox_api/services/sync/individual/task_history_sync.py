"""Individual Task History sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxTaskHistorySyncState
from proxbox_api.services.proxmox_helpers import get_vm_tasks_individual
from proxbox_api.services.sync.individual.base import BaseIndividualSyncService
from proxbox_api.services.sync.task_history import (
    _extract_fk_id,
    _format_task_description,
)


def _normalize_task_datetime(value: object) -> str | None:
    """Normalize task datetime to ISO format string."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
            return dt.isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.isdigit():
            try:
                dt = datetime.fromtimestamp(int(stripped), tz=timezone.utc)
                return dt.isoformat()
            except (OverflowError, OSError, ValueError):
                return None
        return stripped
    return None


def _task_action_label(value: str | None) -> str:
    """Format task type to human-readable label."""
    text = str(value or "").strip()
    if not text:
        return "Task"
    lowered = text.lower()
    for prefix in ("qm", "lxc", "vz"):
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.replace("_", " ").replace("-", " ").strip()
    return text.title() or "Task"


async def sync_task_history_individual(  # noqa: C901
    nb: object,
    px: object,
    tag: object,
    node: str,
    vm_type: str,
    vmid: int,
    upid: str | None = None,
    cluster_name: str | None = None,
    auto_create_vm: bool = True,
    dry_run: bool = False,
) -> dict:
    """Sync a single Task History record from Proxmox to NetBox.

    Args:
        nb: NetBox async session.
        px: Single Proxmox session.
        tag: ProxboxTagDep object.
        node: Proxmox node name.
        vm_type: 'qemu' or 'lxc'.
        vmid: Proxmox VM ID.
        upid: Specific task UPID to sync. If None, syncs the most recent task.
        auto_create_vm: Whether to auto-create the VM if it doesn't exist.
        dry_run: If True, return what would be synced without making changes.

    Returns:
        IndividualSyncResponse dict.
    """
    service = BaseIndividualSyncService(nb, px, tag)
    tag_refs = service.tag_refs
    now = datetime.now(timezone.utc)

    try:
        tasks = get_vm_tasks_individual(px, node, vmid, source="archive")
    except Exception:
        tasks = []

    proxmox_resource: dict[str, object] = {
        "vmid": vmid,
        "node": node,
        "type": vm_type,
        "tasks": tasks,
        "selected_upid": upid,
        "proxmox_last_updated": now.isoformat(),
    }

    target_task = None
    if upid:
        for task in tasks:
            if str(task.get("upid", "")) == upid:
                target_task = task
                break
    elif tasks:
        tasks.sort(key=lambda t: int(t.get("starttime", 0) or 0), reverse=True)
        target_task = tasks[0]

    if not target_task:
        return {
            "object_type": "task_history",
            "action": "noop",
            "proxmox_resource": proxmox_resource,
            "netbox_object": None,
            "dry_run": dry_run,
            "dependencies_synced": [],
            "error": "No task found for the specified criteria" if not dry_run else None,
        }

    task_start_time = _normalize_task_datetime(target_task.get("starttime"))
    task_end_time = _normalize_task_datetime(target_task.get("endtime"))
    task_type = str(target_task.get("type") or "unknown")
    task_id = str(target_task.get("id") or target_task.get("upid", "")[:12])
    task_description = _format_task_description(vm_type, task_id, task_type)

    nb_task_payload: dict[str, object] = {
        "vm_type": vm_type,
        "upid": str(target_task.get("upid", "")),
        "node": node,
        "pid": target_task.get("pid"),
        "pstart": target_task.get("pstart"),
        "task_id": task_id,
        "task_type": task_type,
        "username": str(target_task.get("user", "unknown")),
        "start_time": task_start_time or now.isoformat(),
        "end_time": task_end_time,
        "description": task_description,
        "status": str(target_task.get("exitstatus") or target_task.get("status") or "unknown"),
        "task_state": str(target_task.get("status") or ""),
        "exitstatus": target_task.get("exitstatus"),
        "tags": tag_refs,
        "custom_fields": {},
    }

    target_upid = str(target_task.get("upid", ""))

    if dry_run:
        existing = await rest_list_async(
            nb,
            "/api/plugins/proxbox/task-history/",
            query={"upid": target_upid},
        )
        netbox_object = None
        if existing:
            netbox_object = existing[0].serialize() if hasattr(existing[0], "serialize") else None

        vm_dep: dict[str, object] = {"object_type": "vm", "vmid": vmid}
        return {
            "object_type": "task_history",
            "action": "dry_run",
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": True,
            "dependencies_synced": [vm_dep],
            "error": None,
        }

    try:
        existing_vms = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"cf_proxmox_vm_id": vmid},
        )
        if not existing_vms:
            if auto_create_vm:
                from proxbox_api.services.sync.individual.vm_sync import sync_vm_individual

                await sync_vm_individual(
                    nb,
                    px,
                    tag,
                    cluster_name or getattr(px, "name", "unknown"),
                    node,
                    vm_type,
                    vmid,
                    dry_run=False,
                )
                existing_vms = await rest_list_async(
                    nb,
                    "/api/virtualization/virtual-machines/",
                    query={"cf_proxmox_vm_id": vmid},
                )
            else:
                return {
                    "object_type": "task_history",
                    "action": "error",
                    "proxmox_resource": proxmox_resource,
                    "netbox_object": None,
                    "dry_run": False,
                    "dependencies_synced": [],
                    "error": f"VM with vmid={vmid} not found in NetBox",
                }

        vm_record = existing_vms[0]
        vm_id = getattr(vm_record, "id", None)
        if vm_id is None:
            return {
                "object_type": "task_history",
                "action": "error",
                "proxmox_resource": proxmox_resource,
                "netbox_object": None,
                "dry_run": False,
                "dependencies_synced": [],
                "error": f"Could not resolve VM ID for vmid={vmid}",
            }

        nb_task_payload["virtual_machine"] = vm_id

        existing_records = await rest_list_async(
            nb,
            "/api/plugins/proxbox/task-history/",
            query={"upid": target_upid},
        )
        task_record = await rest_reconcile_async(
            nb,
            "/api/plugins/proxbox/task-history/",
            lookup={"upid": target_upid},
            payload=nb_task_payload,
            schema=NetBoxTaskHistorySyncState,
            current_normalizer=lambda record: {
                "virtual_machine": _extract_fk_id(record.get("virtual_machine")),
                "vm_type": record.get("vm_type"),
                "upid": record.get("upid"),
                "node": record.get("node"),
                "pid": record.get("pid"),
                "pstart": record.get("pstart"),
                "task_id": record.get("task_id"),
                "task_type": record.get("task_type"),
                "username": record.get("username"),
                "start_time": record.get("start_time"),
                "end_time": record.get("end_time"),
                "description": record.get("description"),
                "status": record.get("status"),
                "task_state": record.get("task_state"),
                "exitstatus": record.get("exitstatus"),
                "tags": record.get("tags"),
                "custom_fields": record.get("custom_fields"),
            },
        )

        netbox_object = task_record.serialize() if hasattr(task_record, "serialize") else None
        action = "updated" if existing_records else "created"

        return {
            "object_type": "task_history",
            "action": action,
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": False,
            "dependencies_synced": [{"object_type": "vm", "vmid": vmid, "action": action}],
            "error": None,
        }

    except Exception as error:
        return {
            "object_type": "task_history",
            "action": "error",
            "proxmox_resource": proxmox_resource,
            "netbox_object": None,
            "dry_run": False,
            "dependencies_synced": [],
            "error": str(error),
        }
