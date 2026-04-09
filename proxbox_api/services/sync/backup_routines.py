"""Backup routines sync service for syncing vzdump backup schedules from Proxmox to NetBox."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_bulk_reconcile_async, rest_list_async
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.session.proxmox import ProxmoxSessionsDep

if TYPE_CHECKING:
    from proxbox_api.utils.streaming import WebSocketSSEBridge


def _extract_fk_id(value: object) -> object:
    """Return the integer ID from a nested FK dict, or the value itself."""
    if isinstance(value, dict):
        return value.get("id")
    return value


def _extract_choice_value(value: object) -> object:
    """Return the raw choice string from a nested choice dict, or the value itself."""
    if isinstance(value, dict):
        return value.get("value")
    return value


def _record_id(record) -> int | None:
    return record.id if hasattr(record, "id") else record.get("id")


def _record_get(record, key: str):
    return record.get(key) if hasattr(record, "get") else getattr(record, key, None)


def _match_by_domain(endpoints, px_domain: str) -> int | None:
    for ep in endpoints:
        ep_domain = _record_get(ep, "domain")
        if ep_domain and str(ep_domain).strip() == px_domain.strip():
            return _record_id(ep)
    return None


def _match_by_ip(endpoints, px_ip: str) -> int | None:
    for ep in endpoints:
        ep_ip_field = _record_get(ep, "ip_address")
        if isinstance(ep_ip_field, dict):
            ep_ip_str = ep_ip_field.get("address", "").split("/")[0].strip()
        else:
            ep_ip_str = str(ep_ip_field).split("/")[0].strip() if ep_ip_field else ""
        if ep_ip_str and ep_ip_str == px_ip.strip():
            return _record_id(ep)
    return None


def _match_by_name(endpoints, px_name: str) -> int | None:
    for ep in endpoints:
        ep_name = _record_get(ep, "name")
        if ep_name and str(ep_name).strip() == px_name.strip():
            return _record_id(ep)
    return None


async def _get_netbox_endpoint_id(nb, px) -> int | None:
    """Look up the NetBox plugin ProxmoxEndpoint integer ID for a Proxmox session.

    Tries multiple strategies in order:
    1. Match by domain string (exact)
    2. Match by IP address string
    3. Match by name string
    4. If exactly one endpoint exists, use it (common single-cluster setup)
    """
    try:
        all_endpoints = await rest_list_async(
            nb,
            "/api/plugins/proxbox/endpoints/proxmox/",
        )
    except Exception as exc:
        logger.warning(
            "Could not list NetBox ProxmoxEndpoints for session '%s': %s",
            getattr(px, "name", "unknown"),
            exc,
        )
        return None

    if not all_endpoints:
        logger.warning(
            "No ProxmoxEndpoints found in NetBox; cannot resolve endpoint for session '%s'",
            getattr(px, "name", "unknown"),
        )
        return None

    px_domain = str(getattr(px, "domain", None) or "")
    px_ip = str(getattr(px, "ip_address", None) or "")
    px_name = str(getattr(px, "name", None) or "")

    if px_domain:
        found = _match_by_domain(all_endpoints, px_domain)
        if found is not None:
            return found

    if px_ip:
        found = _match_by_ip(all_endpoints, px_ip)
        if found is not None:
            return found

    if px_name:
        found = _match_by_name(all_endpoints, px_name)
        if found is not None:
            return found

    if len(all_endpoints) == 1:
        logger.info(
            "Resolving ProxmoxEndpoint for session '%s' via single-endpoint fallback",
            px_name,
        )
        return _record_id(all_endpoints[0])

    logger.warning(
        "Could not resolve NetBox ProxmoxEndpoint for session '%s' "
        "(domain=%r, ip=%r); backup routines for this session will be skipped.",
        px_name,
        px_domain or None,
        px_ip or None,
    )
    return None


async def _fetch_session_payloads(px, nb, results: dict) -> list[dict]:  # noqa: C901
    """Fetch backup routine payloads for a single Proxmox session."""
    try:
        backup_jobs = await resolve_async(px.session.cluster.backup.get())
    except Exception as e:
        logger.warning("Error fetching backup routines for %s: %s", px.name, e)
        return []

    netbox_endpoint_id = await _get_netbox_endpoint_id(nb, px)
    if netbox_endpoint_id is None:
        logger.warning(
            "Skipping backup routines for Proxmox session '%s': "
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

            payloads.append({
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
            })
        except Exception as e:
            logger.warning("Error building payload for backup routine %s: %s", job.get("id"), e)
            results["errors"] += 1

    return payloads


async def sync_all_backup_routines(
    netbox_session,
    pxs: ProxmoxSessionsDep,
    *,
    bridge: "WebSocketSSEBridge | None" = None,
) -> dict:
    """Sync all backup routines (vzdump schedules) from Proxmox to NetBox using bulk operations.

    Args:
        netbox_session: NetBox async session.
        pxs: Proxmox sessions dependency.
        bridge: Optional SSE bridge for streaming progress events.

    Returns:
        Dict with sync results (created, updated, errors counts).
    """
    nb = netbox_session
    results: dict = {"created": 0, "updated": 0, "errors": 0}

    fetch_tasks = [_fetch_session_payloads(px, nb, results) for px in pxs]
    all_payloads_by_session = await asyncio.gather(*fetch_tasks, return_exceptions=True)

    all_payloads: list[dict] = []
    for payload_list in all_payloads_by_session:
        if isinstance(payload_list, list):
            all_payloads.extend(payload_list)

    if not all_payloads:
        logger.info("No backup routines to sync")
        if bridge:
            await bridge.emit_phase_summary(
                phase="backup-routines",
                message="No backup routines found in Proxmox",
            )
        return results

    # Emit discovery so the live panel shows how many items will be processed
    if bridge:
        await bridge.emit_discovery(
            phase="backup-routines",
            items=[
                {"name": str(p.get("job_id", "")), "type": "backup-routine"}
                for p in all_payloads
            ],
            message=f"Discovered {len(all_payloads)} backup routine(s) to synchronize",
        )

    try:
        reconcile_result = await rest_bulk_reconcile_async(
            nb,
            "/api/plugins/proxbox/backup-routines/",
            payloads=all_payloads,
            lookup_fields=["job_id"],
            schema=dict,
            current_normalizer=lambda record: {
                "job_id": record.get("job_id"),
                "endpoint": _extract_fk_id(record.get("endpoint")),
                "enabled": record.get("enabled"),
                "schedule": record.get("schedule"),
                "selection": record.get("selection"),
                "status": _extract_choice_value(record.get("status")),
            },
        )

        results["created"] = reconcile_result.created
        results["updated"] = reconcile_result.updated
        results["errors"] += reconcile_result.failed
        logger.info(
            "Backup routines sync completed: created=%s, updated=%s, failed=%s",
            reconcile_result.created,
            reconcile_result.updated,
            reconcile_result.failed,
        )

        if bridge:
            await bridge.emit_phase_summary(
                phase="backup-routines",
                created=reconcile_result.created,
                updated=reconcile_result.updated,
                failed=reconcile_result.failed,
                message=(
                    f"Backup routines sync completed: {reconcile_result.created} created, "
                    f"{reconcile_result.updated} updated, {reconcile_result.failed} failed"
                ),
            )

    except Exception as e:
        logger.error("Error during bulk backup routines reconciliation: %s", e, exc_info=True)
        results["errors"] = len(all_payloads)
        if bridge:
            await bridge.emit_error_detail(
                message=f"Backup routines bulk reconciliation failed: {e}",
                category="internal",
                phase="backup-routines",
                detail=str(e),
            )

    return results
