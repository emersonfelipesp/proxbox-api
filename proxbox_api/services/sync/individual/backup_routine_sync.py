"""Individual Backup Routines sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_async import resolve_async
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

    if dry_run:
        existing = await rest_list_async(
            nb,
            "/api/plugins/proxbox/backup-routines/",
            query={"job_id": job_id},
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
        job_payload: dict[str, object] = {
            "job_id": job_id,
            "enabled": target_job.get("enabled", True),
            "schedule": target_job.get("schedule", ""),
            "node": target_job.get("node"),
            "storage": target_job.get("storage", ""),
            "selection": target_job.get("selection", []),
            "keep_last": target_job.get("keep_last"),
            "keep_daily": target_job.get("keep_daily"),
            "keep_weekly": target_job.get("keep_weekly"),
            "keep_monthly": target_job.get("keep_monthly"),
            "keep_yearly": target_job.get("keep_yearly"),
            "keep_all": target_job.get("keep_all"),
            "bwlimit": target_job.get("bwlimit"),
            "zstd": target_job.get("zstd"),
            "io_workers": target_job.get("io_workers"),
            "fleecing": target_job.get("fleecing"),
            "fleecing_storage": target_job.get("fleecing_storage"),
            "repeat_missed": target_job.get("repeat_missed"),
            "pbs_change_detection_mode": target_job.get("pbs_change_detection_mode"),
            "raw_config": target_job,
            "status": "active",
            "tags": tag_refs,
        }

        existing = await rest_list_async(
            nb,
            "/api/plugins/proxbox/backup-routines/",
            query={"job_id": job_id},
        )

        routine_record = await rest_reconcile_async(
            nb,
            "/api/plugins/proxbox/backup-routines/",
            lookup={"job_id": job_id},
            payload=job_payload,
            schema=dict,
            current_normalizer=lambda record: {
                "job_id": record.get("job_id"),
                "enabled": record.get("enabled"),
                "schedule": record.get("schedule"),
                "node": record.get("node"),
                "storage": record.get("storage"),
                "selection": record.get("selection"),
                "status": record.get("status"),
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
