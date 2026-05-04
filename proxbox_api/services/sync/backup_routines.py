"""Backup routines sync service for syncing vzdump backup schedules from Proxmox to NetBox."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    rest_bulk_patch_async,
    rest_bulk_reconcile_async,
    rest_list_async,
    rest_list_paginated_async,
)
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.services.sync._helpers import _extract_choice_value, _extract_fk_id
from proxbox_api.session.proxmox import ProxmoxSessionsDep

if TYPE_CHECKING:
    from proxbox_api.utils.streaming import WebSocketSSEBridge


def _coerce_kv_string(value: object) -> str | None:
    """Convert a dict of key=value pairs to a comma-separated string, or pass through strings."""
    if value is None:
        return None
    if isinstance(value, dict):
        return ",".join(f"{k}={v}" for k, v in value.items())
    return str(value)


def _parse_vmid_selection(vmid_raw: object) -> list[int]:
    """Parse Proxmox vmid field into a list of integer VMIDs."""
    if not vmid_raw:
        return []
    if isinstance(vmid_raw, str):
        return [int(part.strip()) for part in vmid_raw.split(",") if part.strip().isdigit()]
    if isinstance(vmid_raw, list):
        return [int(x) for x in vmid_raw if str(x).isdigit()]
    return []


def _parse_retention(job: dict) -> dict:  # noqa: C901
    """Parse Proxmox prune-backups string into individual retention fields."""
    raw = job.get("prune-backups") or job.get("prune_backups") or ""
    result: dict[str, int | bool | None] = {
        "keep_last": None,
        "keep_daily": None,
        "keep_weekly": None,
        "keep_monthly": None,
        "keep_yearly": None,
        "keep_all": None,
    }
    if not raw:
        return result
    if isinstance(raw, dict):
        # proxmoxer already parsed the key=value string into a dict
        mapping = {
            "keep-last": "keep_last",
            "keep-daily": "keep_daily",
            "keep-weekly": "keep_weekly",
            "keep-monthly": "keep_monthly",
            "keep-yearly": "keep_yearly",
        }
        for px_key, nb_key in mapping.items():
            val = raw.get(px_key)
            if val is not None:
                result[nb_key] = int(val) if str(val).isdigit() else None
        if "keep-all" in raw:
            result["keep_all"] = True
        return result
    for part in raw.split(","):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key == "keep-last":
                result["keep_last"] = int(value) if value.isdigit() else None
            elif key == "keep-daily":
                result["keep_daily"] = int(value) if value.isdigit() else None
            elif key == "keep-weekly":
                result["keep_weekly"] = int(value) if value.isdigit() else None
            elif key == "keep-monthly":
                result["keep_monthly"] = int(value) if value.isdigit() else None
            elif key == "keep-yearly":
                result["keep_yearly"] = int(value) if value.isdigit() else None
        elif part.lower() == "keep-all":
            result["keep_all"] = True
    return result


def _record_id(record: object) -> int | None:
    return record.id if hasattr(record, "id") else record.get("id")  # type: ignore[union-attr]


def _record_get(record: object, key: str) -> object:
    return record.get(key) if hasattr(record, "get") else getattr(record, key, None)  # type: ignore[union-attr]


def _match_by_domain(endpoints: object, px_domain: str) -> int | None:
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

            retention = _parse_retention(job)
            payloads.append(
                {
                    "job_id": job_id,
                    "endpoint": netbox_endpoint_id,
                    "enabled": bool(job.get("enabled", True)),
                    "schedule": job.get("schedule") or "",
                    "comment": job.get("comment") or "",
                    "notes_template": job.get("notes-template") or job.get("notes_template") or "",
                    # node/storage are nullable FKs — send null rather than raw
                    # Proxmox name strings, which DRF would reject as invalid PKs.
                    "node": None,
                    "storage": None,
                    "fleecing_storage": None,
                    # Proxmox uses vmid for VM ID list; selection is the selection type string
                    "selection": _parse_vmid_selection(job.get("vmid")),
                    "keep_last": retention["keep_last"],
                    "keep_daily": retention["keep_daily"],
                    "keep_weekly": retention["keep_weekly"],
                    "keep_monthly": retention["keep_monthly"],
                    "keep_yearly": retention["keep_yearly"],
                    "keep_all": retention["keep_all"],
                    "bwlimit": job.get("bwlimit"),
                    "zstd": job.get("zstd"),
                    "io_workers": job.get("io_workers"),
                    "fleecing": _coerce_kv_string(job.get("fleecing")),
                    "repeat_missed": job.get("repeat-missed")
                    if "repeat-missed" in job
                    else job.get("repeat_missed"),
                    "pbs_change_detection_mode": job.get("pbs-change-detection-mode")
                    if "pbs-change-detection-mode" in job
                    else job.get("pbs_change_detection_mode"),
                    "raw_config": job,
                    "status": "active",
                }
            )
        except Exception as e:
            logger.warning("Error building payload for backup routine %s: %s", job.get("id"), e)
            results["errors"] += 1

    return payloads


async def _mark_stale_routines(nb: object, synced_payloads: list[dict]) -> int:
    """PATCH any existing backup routine not in synced_payloads to status='stale'."""
    synced_keys = {(p["endpoint"], p["job_id"]) for p in synced_payloads}
    try:
        all_existing = await rest_list_paginated_async(nb, "/api/plugins/proxbox/backup-routines/")
        stale_updates = []
        for record in all_existing:
            serialized = record.serialize()
            ep_id = _extract_fk_id(serialized.get("endpoint"))
            job_id = serialized.get("job_id")
            current_status = _extract_choice_value(serialized.get("status"))
            record_id = serialized.get("id")
            if record_id and (ep_id, job_id) not in synced_keys and current_status != "stale":
                stale_updates.append({"id": record_id, "status": "stale"})
        if stale_updates:
            await rest_bulk_patch_async(nb, "/api/plugins/proxbox/backup-routines/", stale_updates)
            logger.info("Marked %s backup routine(s) as stale", len(stale_updates))
        return len(stale_updates)
    except Exception as exc:
        logger.warning("Failed to mark stale backup routines: %s", exc)
        return 0


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
    results: dict = {"created": 0, "updated": 0, "stale": 0, "errors": 0}

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
                {"name": str(p.get("job_id", "")), "type": "backup-routine"} for p in all_payloads
            ],
            message=f"Discovered {len(all_payloads)} backup routine(s) to synchronize",
        )

    try:
        reconcile_result = await rest_bulk_reconcile_async(
            nb,
            "/api/plugins/proxbox/backup-routines/",
            payloads=all_payloads,
            lookup_fields=["job_id", "endpoint"],
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

        results["created"] = reconcile_result.created
        results["updated"] = reconcile_result.updated
        results["errors"] += reconcile_result.failed
        results["stale"] = await _mark_stale_routines(nb, all_payloads)

        logger.info(
            "Backup routines sync completed: created=%s, updated=%s, stale=%s, failed=%s",
            reconcile_result.created,
            reconcile_result.updated,
            results["stale"],
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
                    f"{reconcile_result.updated} updated, {results['stale']} stale, "
                    f"{reconcile_result.failed} failed"
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
