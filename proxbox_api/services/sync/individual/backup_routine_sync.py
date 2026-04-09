"""Individual Backup Routines sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.services.sync.backup_routines import (
    _extract_choice_value,
    _extract_fk_id,
    _get_netbox_endpoint_id,
    _parse_retention,
    _parse_vmid_selection,
)
from proxbox_api.services.sync.individual.base import BaseIndividualSyncService


async def sync_backup_routine_individual(
    nb: object,
    px: object,
    tag: object,
    job_id: str,
    dry_run: bool = False,
) -> dict:
    """Sync a single Backup Routine from Proxmox to NetBox.

    Args:
        nb: NetBox async session.
        px: Single Proxmox session.
        tag: ProxboxTagDep object.
        job_id: Backup job ID (e.g., 'backup:weekly').
        dry_run: If True, return what would be synced without making changes.

    Returns:
        IndividualSyncResponse dict.
    """
    service = BaseIndividualSyncService(nb, px, tag)
    tag_refs = service.tag_refs
    now = datetime.now(timezone.utc)

    try:
        backup_jobs = await resolve_async(px.session.cluster.backup.get())
    except Exception:
        backup_jobs = []

    target_job = None
    for job in backup_jobs:
        if str(job.get("id", "")) == job_id:
            target_job = job
            break

    if not target_job:
        return {
            "object_type": "backup_routine",
            "action": "error",
            "proxmox_resource": {"job_id": job_id},
            "netbox_object": None,
            "dry_run": False,
            "dependencies_synced": [],
            "error": f"Backup routine {job_id} not found in Proxmox",
        }

    proxmox_resource: dict[str, object] = {
        "job_id": job_id,
        "backup_data": target_job,
        "proxmox_last_updated": now.isoformat(),
    }

    netbox_endpoint_id = await _get_netbox_endpoint_id(nb, px)
    if netbox_endpoint_id is None:
        return {
            "object_type": "backup_routine",
            "action": "error",
            "proxmox_resource": proxmox_resource,
            "netbox_object": None,
            "dry_run": False,
            "dependencies_synced": [],
            "error": f"No matching ProxmoxEndpoint for Proxmox session '{getattr(px, 'name', 'unknown')}'",
        }

    if dry_run:
        existing = await rest_list_async(
            nb,
            "/api/plugins/proxbox/backup-routines/",
            query={"job_id": job_id, "endpoint": netbox_endpoint_id},
        )
        netbox_object = None
        if existing:
            netbox_object = existing[0].serialize() if hasattr(existing[0], "serialize") else None

        return {
            "object_type": "backup_routine",
            "action": "dry_run",
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": True,
            "dependencies_synced": [],
            "error": None,
        }

    try:
        retention = _parse_retention(target_job)
        job_payload: dict[str, object] = {
            "job_id": job_id,
            "endpoint": netbox_endpoint_id,
            "enabled": bool(target_job.get("enabled", True)),
            "schedule": target_job.get("schedule") or "",
            # node/storage are nullable FKs — send null rather than raw Proxmox
            # name strings, which DRF would reject as invalid PKs.
            "node": None,
            "storage": None,
            "fleecing_storage": None,
            "selection": _parse_vmid_selection(target_job.get("vmid")),
            "keep_last": retention["keep_last"],
            "keep_daily": retention["keep_daily"],
            "keep_weekly": retention["keep_weekly"],
            "keep_monthly": retention["keep_monthly"],
            "keep_yearly": retention["keep_yearly"],
            "keep_all": retention["keep_all"],
            "bwlimit": target_job.get("bwlimit"),
            "zstd": target_job.get("zstd"),
            "io_workers": target_job.get("io_workers"),
            "fleecing": target_job.get("fleecing"),
            "repeat_missed": target_job.get("repeat-missed") if "repeat-missed" in target_job else target_job.get("repeat_missed"),
            "pbs_change_detection_mode": target_job.get("pbs-change-detection-mode") if "pbs-change-detection-mode" in target_job else target_job.get("pbs_change_detection_mode"),
            "raw_config": target_job,
            "status": "active",
            "tags": tag_refs,
        }

        existing = await rest_list_async(
            nb,
            "/api/plugins/proxbox/backup-routines/",
            query={"job_id": job_id, "endpoint": netbox_endpoint_id},
        )

        routine_record = await rest_reconcile_async(
            nb,
            "/api/plugins/proxbox/backup-routines/",
            lookup={"job_id": job_id, "endpoint": netbox_endpoint_id},
            payload=job_payload,
            schema=dict,
            current_normalizer=lambda record: {
                "job_id": record.get("job_id"),
                "endpoint": _extract_fk_id(record.get("endpoint")),
                "enabled": record.get("enabled"),
                "schedule": record.get("schedule"),
                "selection": record.get("selection"),
                "keep_last": record.get("keep_last"),
                "keep_daily": record.get("keep_daily"),
                "keep_weekly": record.get("keep_weekly"),
                "keep_monthly": record.get("keep_monthly"),
                "keep_yearly": record.get("keep_yearly"),
                "keep_all": record.get("keep_all"),
                "bwlimit": record.get("bwlimit"),
                "zstd": record.get("zstd"),
                "io_workers": record.get("io_workers"),
                "fleecing": record.get("fleecing"),
                "repeat_missed": record.get("repeat_missed"),
                "pbs_change_detection_mode": record.get("pbs_change_detection_mode"),
                "raw_config": record.get("raw_config"),
                "status": _extract_choice_value(record.get("status")),
            },
        )

        netbox_object = routine_record.serialize() if hasattr(routine_record, "serialize") else None
        action = "updated" if existing else "created"

        return {
            "object_type": "backup_routine",
            "action": action,
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": False,
            "dependencies_synced": [],
            "error": None,
        }

    except Exception as error:
        return {
            "object_type": "backup_routine",
            "action": "error",
            "proxmox_resource": proxmox_resource,
            "netbox_object": None,
            "dry_run": False,
            "dependencies_synced": [],
            "error": str(error),
        }
