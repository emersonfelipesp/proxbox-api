"""Backup routines sync service for syncing vzdump backup schedules from Proxmox to NetBox."""

from __future__ import annotations

import asyncio

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_bulk_reconcile_async, rest_list_async
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.session.proxmox import ProxmoxSessionsDep


async def _get_netbox_endpoint_id(nb, endpoint_name: str) -> int | None:
    """Look up the NetBox plugin ProxmoxEndpoint integer ID by name."""
    try:
        results = await rest_list_async(
            nb,
            "/api/plugins/proxbox/endpoints/proxmox/",
            query={"name": endpoint_name},
        )
        if results:
            record = results[0]
            return record.id if hasattr(record, "id") else record.get("id")
    except Exception as exc:
        logger.warning(
            "Could not resolve NetBox ProxmoxEndpoint ID for '%s': %s",
            endpoint_name,
            exc,
        )
    return None


async def sync_all_backup_routines(
    netbox_session,
    pxs: ProxmoxSessionsDep,
) -> dict:
    """Sync all backup routines (vzdump schedules) from Proxmox to NetBox using bulk operations.

    Args:
        netbox_session: NetBox async session.
        pxs: Proxmox sessions dependency.

    Returns:
        Dict with sync results (created, updated, errors counts).
    """
    nb = netbox_session
    results = {"created": 0, "updated": 0, "errors": 0}

    async def fetch_backup_routines_for_session(px):
        """Fetch backup routines for a single Proxmox session. Returns list of job payloads."""
        try:
            backup_jobs = await resolve_async(px.session.cluster.backup.get())
        except Exception as e:
            logger.warning("Error fetching backup routines for %s: %s", px.name, e)
            return []

        # Resolve the NetBox plugin ProxmoxEndpoint ID for this session.
        # The endpoint FK is required; skip this session's routines if it can't
        # be resolved rather than sending 400-producing payloads.
        netbox_endpoint_id = await _get_netbox_endpoint_id(nb, px.name)
        if netbox_endpoint_id is None:
            logger.warning(
                "Skipping backup routines for Proxmox endpoint '%s': "
                "no matching ProxmoxEndpoint found in NetBox plugin.",
                px.name,
            )
            return []

        payloads = []
        for job in backup_jobs:
            try:
                job_id = job.get("id", "")
                if not job_id:
                    continue

                job_payload = {
                    "job_id": job_id,
                    "endpoint": netbox_endpoint_id,
                    "enabled": job.get("enabled", True),
                    "schedule": job.get("schedule", ""),
                    # node/storage are nullable FKs — send null rather than raw
                    # Proxmox name strings, which DRF would reject as invalid PKs.
                    "node": None,
                    "storage": None,
                    "fleecing_storage": None,
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
                    "repeat_missed": job.get("repeat_missed"),
                    "pbs_change_detection_mode": job.get("pbs_change_detection_mode"),
                    "raw_config": job,
                    "status": "active",
                }
                payloads.append(job_payload)

            except Exception as e:
                logger.warning("Error building payload for backup routine %s: %s", job.get("id"), e)
                results["errors"] += 1

        return payloads

    # Fetch backup routines from all Proxmox sessions in parallel
    fetch_tasks = [fetch_backup_routines_for_session(px) for px in pxs]
    all_payloads_by_session = await asyncio.gather(*fetch_tasks, return_exceptions=True)

    # Flatten all payloads from all sessions
    all_payloads = []
    for payload_list in all_payloads_by_session:
        if isinstance(payload_list, list):
            all_payloads.extend(payload_list)

    if not all_payloads:
        logger.info("No backup routines to sync")
        return results

    try:
        # Perform bulk reconciliation with a single API call
        reconcile_result = await rest_bulk_reconcile_async(
            nb,
            "/api/plugins/proxbox/backup-routines/",
            payloads=all_payloads,
            lookup_fields=["job_id"],
            schema=dict,
            current_normalizer=lambda record: {
                "job_id": record.get("job_id"),
                "endpoint": record.get("endpoint"),
                "enabled": record.get("enabled"),
                "schedule": record.get("schedule"),
                "selection": record.get("selection"),
                "status": record.get("status"),
            },
        )

        results["created"] = reconcile_result.created
        results["updated"] = reconcile_result.updated
        results["errors"] += reconcile_result.failed
        logger.info(
            "Backup routines sync completed: created=%s, updated=%s",
            reconcile_result.created,
            reconcile_result.updated,
        )

    except Exception as e:
        logger.error("Error during bulk backup routines reconciliation: %s", e, exc_info=True)
        results["errors"] = len(all_payloads)

    return results
