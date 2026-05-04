"""Replication sync service for syncing replications from Proxmox to NetBox."""

from __future__ import annotations

import asyncio

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    rest_bulk_patch_async,
    rest_bulk_reconcile_async,
    rest_list_async,
    rest_list_paginated_async,
)
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.services.sync._helpers import _extract_choice_value, _extract_fk_id
from proxbox_api.services.sync.backup_routines import _get_netbox_endpoint_id
from proxbox_api.session.proxmox import ProxmoxSessionsDep


async def _mark_stale_replications(
    nb: object,
    synced_replication_ids: set[str],
    endpoint_id: int | None,
) -> int:
    """Mark replication records no longer found in Proxmox as stale.

    Args:
        nb: NetBox async session.
        synced_replication_ids: Set of replication_id values successfully synced.
        endpoint_id: NetBox ProxmoxEndpoint ID to scope the query.

    Returns:
        Count of records marked stale.
    """
    query: dict[str, object] = {"status": "active"}
    if endpoint_id:
        query["endpoint"] = endpoint_id

    try:
        active_records = await rest_list_paginated_async(
            nb,
            "/api/plugins/proxbox/replications/",
            base_query=query,
        )
    except Exception as e:
        logger.warning("Error fetching active replication records for stale check: %s", e)
        return 0

    stale_ids = [
        r.get("id")
        for r in (active_records or [])
        if r.get("replication_id") not in synced_replication_ids and r.get("id")
    ]

    if not stale_ids:
        return 0

    try:
        await rest_bulk_patch_async(
            nb,
            "/api/plugins/proxbox/replications/",
            updates=[{"id": rid, "status": "stale"} for rid in stale_ids],
        )
        logger.info("Marked %d replication records as stale", len(stale_ids))
    except Exception as e:
        logger.warning("Error marking stale replication records: %s", e)
        return 0

    return len(stale_ids)


async def sync_all_replications(  # noqa: C901
    netbox_session: object,
    pxs: ProxmoxSessionsDep,
    tag_refs: list[dict[str, object]] | None = None,
) -> dict[str, int]:
    """Sync all replication jobs from Proxmox to NetBox using bulk operations.

    Args:
        netbox_session: NetBox async session.
        pxs: Proxmox sessions dependency.
        tag_refs: Optional list of tag reference dicts to attach to synced records.

    Returns:
        Dict with sync results (created, updated, stale, errors counts).
    """
    nb = netbox_session
    results: dict[str, int] = {"created": 0, "updated": 0, "stale": 0, "errors": 0}
    _tag_refs = tag_refs or []

    # Pre-fetch all VMs once, indexed by proxmox_vm_id
    try:
        all_vms = await rest_list_async(nb, "/api/virtualization/virtual-machines/")
        vms_by_proxmox_id = {}
        for vm in all_vms or []:
            proxmox_id = vm.get("custom_fields", {}).get("proxmox_vm_id")
            if proxmox_id:
                vms_by_proxmox_id[str(proxmox_id)] = vm.get("id")
    except Exception as e:
        logger.warning("Error pre-fetching NetBox VMs: %s", e)
        vms_by_proxmox_id = {}

    # Pre-fetch all ProxmoxNode records from the NetBox plugin, indexed by name
    try:
        all_nodes = await rest_list_async(nb, "/api/plugins/proxbox/nodes/")
        nodes_by_name: dict[str, int] = {}
        for node in all_nodes or []:
            name = node.get("name")
            node_id = node.get("id")
            if name and node_id:
                nodes_by_name[str(name)] = int(node_id)
    except Exception as e:
        logger.warning("Error pre-fetching NetBox ProxmoxNodes: %s", e)
        nodes_by_name = {}

    async def fetch_replications_for_session(
        px: object,
    ) -> tuple[int | None, list[dict[str, object]]]:
        """Fetch replications for a single Proxmox session. Returns (endpoint_id, payloads)."""
        endpoint_id = await _get_netbox_endpoint_id(nb, px)

        try:
            replications = await resolve_async(px.session.cluster.replication.get())
        except Exception as e:
            logger.warning("Error fetching replications for %s: %s", px.name, e)
            return endpoint_id, []

        payloads: list[dict[str, object]] = []
        for rep in replications:
            try:
                guest_vmid = rep.get("guest")
                if not guest_vmid:
                    continue

                # Look up NetBox VM using pre-fetched cache
                netbox_vm_id = vms_by_proxmox_id.get(str(guest_vmid))
                if not netbox_vm_id:
                    logger.debug(
                        "VM with proxmox_vm_id=%s not found in NetBox, skipping replication",
                        guest_vmid,
                    )
                    continue

                target_node_name = rep.get("target") or ""
                replication_payload = {
                    "replication_id": rep.get("id", ""),
                    "endpoint": endpoint_id,
                    "guest": rep.get("guest"),
                    "target": target_node_name,
                    "job_type": rep.get("type"),
                    "schedule": rep.get("schedule"),
                    "rate": rep.get("rate"),
                    "comment": rep.get("comment"),
                    "disable": rep.get("disable"),
                    "source": rep.get("source"),
                    "jobnum": rep.get("jobnum"),
                    "remove_job": rep.get("remove_job"),
                    "virtual_machine": netbox_vm_id,
                    "proxmox_node": nodes_by_name.get(target_node_name),
                    "raw_config": rep,
                    "status": "active",
                    "tags": _tag_refs,
                }
                payloads.append(replication_payload)

            except Exception as e:
                logger.warning("Error building payload for replication %s: %s", rep.get("id"), e)
                results["errors"] += 1

        return endpoint_id, payloads

    # Fetch replications from all Proxmox sessions in parallel
    fetch_tasks = [fetch_replications_for_session(px) for px in pxs]
    all_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

    # Flatten all payloads from all sessions, collect per-endpoint IDs
    all_payloads: list[dict[str, object]] = []
    endpoint_ids: set[int] = set()
    for result in all_results:
        if isinstance(result, tuple):
            ep_id, payload_list = result
            if isinstance(payload_list, list):
                all_payloads.extend(payload_list)
            if ep_id:
                endpoint_ids.add(ep_id)

    if not all_payloads:
        logger.info("No replications to sync")
        return results

    try:
        # Perform bulk reconciliation with a single API call
        reconcile_result = await rest_bulk_reconcile_async(
            nb,
            "/api/plugins/proxbox/replications/",
            payloads=all_payloads,
            lookup_fields=["replication_id", "endpoint"],
            schema=dict,
            current_normalizer=lambda record: {
                "replication_id": record.get("replication_id"),
                "endpoint": _extract_fk_id(record.get("endpoint")),
                "guest": record.get("guest"),
                "target": record.get("target"),
                "job_type": _extract_choice_value(record.get("job_type")),
                "schedule": record.get("schedule"),
                "rate": record.get("rate"),
                "comment": record.get("comment"),
                "disable": record.get("disable"),
                "source": record.get("source"),
                "jobnum": record.get("jobnum"),
                "remove_job": _extract_choice_value(record.get("remove_job")),
                "virtual_machine": _extract_fk_id(record.get("virtual_machine")),
                "proxmox_node": _extract_fk_id(record.get("proxmox_node")),
                "raw_config": record.get("raw_config"),
                "status": _extract_choice_value(record.get("status")),
            },
        )

        results["created"] = reconcile_result.created
        results["updated"] = reconcile_result.updated
        results["errors"] += reconcile_result.failed
        logger.info(
            "Replication sync completed: created=%s, updated=%s",
            reconcile_result.created,
            reconcile_result.updated,
        )

    except Exception as e:
        logger.error("Error during bulk replication reconciliation: %s", e)
        results["errors"] = len(all_payloads)
        return results

    # Mark stale: replications that exist in NetBox but were not returned by Proxmox
    synced_ids = {p["replication_id"] for p in all_payloads}
    for ep_id in endpoint_ids:
        stale_count = await _mark_stale_replications(nb, synced_ids, ep_id)
        results["stale"] += stale_count

    return results
