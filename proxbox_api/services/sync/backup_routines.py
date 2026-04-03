"""Backup routines sync service for syncing vzdump backup schedules from Proxmox to NetBox."""

from __future__ import annotations

import asyncio

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.session.proxmox import ProxmoxSessionsDep


async def sync_all_backup_routines(
    netbox_session,
    pxs: ProxmoxSessionsDep,
) -> dict:
    """Sync all backup routines (vzdump schedules) from Proxmox to NetBox.

    Args:
        netbox_session: NetBox async session.
        pxs: Proxmox sessions dependency.

    Returns:
        Dict with sync results (created, updated, errors counts).
    """
    nb = netbox_session
    results = {"created": 0, "updated": 0, "errors": 0}

    async def sync_backup_routines_for_session(px):
        """Sync backup routines for a single Proxmox session."""
        session_results = {"created": 0, "updated": 0, "errors": 0}
        try:
            backup_jobs = px.session.cluster.backup.get()
        except Exception as e:
            logger.warning("Error fetching backup routines for %s: %s", px.name, e)
            return session_results

        for job in backup_jobs:
            try:
                job_id = job.get("id", "")
                if not job_id:
                    continue

                job_payload = {
                    "job_id": job_id,
                    "enabled": job.get("enabled", True),
                    "schedule": job.get("schedule", ""),
                    "node": job.get("node"),
                    "storage": job.get("storage", ""),
                    "selection": job.get("selection", []),
                    "keep_last": job.get("keep_last"),
                    "keep_daily": job.get("keep_daily"),
                    "keep_weekly": job.get("keep_weekly"),
                    "keep_monthly": job.get("keep_monthly"),
                    "keep_yearly": job.get("keep_yearly"),
                    "keep_all": job.get("keep_all"),
                    "bwlimit": job.get("bwlimit"),
                    "zstd": job.get("zstd"),
                    "io_workers": job.get("io_workers"),
                    "fleecing": job.get("fleecing"),
                    "fleecing_storage": job.get("fleecing_storage"),
                    "repeat_missed": job.get("repeat_missed"),
                    "pbs_change_detection_mode": job.get("pbs_change_detection_mode"),
                    "raw_config": job,
                    "status": "active",
                }

                existing = await rest_list_async(
                    nb,
                    "/api/plugins/proxbox/backup-routines/",
                    query={"job_id": job_id},
                )

                if existing:
                    await rest_reconcile_async(
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
                    session_results["updated"] += 1
                else:
                    await rest_reconcile_async(
                        nb,
                        "/api/plugins/proxbox/backup-routines/",
                        lookup={"job_id": job_id},
                        payload=job_payload,
                        schema=dict,
                        current_normalizer=lambda record: {},
                    )
                    session_results["created"] += 1

            except Exception as e:
                logger.warning("Error syncing backup routine %s: %s", job.get("id"), e)
                session_results["errors"] += 1

        return session_results

    tasks = [sync_backup_routines_for_session(px) for px in pxs]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in all_results:
        if isinstance(r, dict):
            results["created"] += r.get("created", 0)
            results["updated"] += r.get("updated", 0)
            results["errors"] += r.get("errors", 0)

    logger.info("Backup routines sync completed: %s", results)
    return results
