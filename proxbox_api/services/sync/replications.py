"""Replication sync service for syncing replications from Proxmox to NetBox."""

from __future__ import annotations

import asyncio

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_bulk_reconcile_async, rest_list_async
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.session.proxmox import ProxmoxSessionsDep


async def sync_all_replications(  # noqa: C901
    netbox_session,
    pxs: ProxmoxSessionsDep,
) -> dict:
    """Sync all replication jobs from Proxmox to NetBox using bulk operations.

    Args:
        netbox_session: NetBox async session.
        pxs: Proxmox sessions dependency.

    Returns:
        Dict with sync results (created, updated, errors counts).
    """
    nb = netbox_session
    results = {"created": 0, "updated": 0, "errors": 0}

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

    async def fetch_replications_for_session(px):
        """Fetch replications for a single Proxmox session. Returns list of replication payloads."""
        try:
            replications = await resolve_async(px.session.cluster.replication.get())
        except Exception as e:
            logger.warning("Error fetching replications for %s: %s", px.name, e)
            return []

        payloads = []
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

                replication_payload = {
                    "replication_id": rep.get("id", ""),
                    "guest": rep.get("guest"),
                    "target": rep.get("target"),
                    "job_type": rep.get("type"),
                    "schedule": rep.get("schedule"),
                    "rate": rep.get("rate"),
                    "comment": rep.get("comment"),
                    "disable": rep.get("disable"),
                    "source": rep.get("source"),
                    "jobnum": rep.get("jobnum"),
                    "remove_job": rep.get("remove_job"),
                    "virtual_machine": netbox_vm_id,
                }
                payloads.append(replication_payload)

            except Exception as e:
                logger.warning("Error building payload for replication %s: %s", rep.get("id"), e)
                results["errors"] += 1

        return payloads

    # Fetch replications from all Proxmox sessions in parallel
    fetch_tasks = [fetch_replications_for_session(px) for px in pxs]
    all_payloads_by_session = await asyncio.gather(*fetch_tasks, return_exceptions=True)

    # Flatten all payloads from all sessions
    all_payloads = []
    for payload_list in all_payloads_by_session:
        if isinstance(payload_list, list):
            all_payloads.extend(payload_list)

    if not all_payloads:
        logger.info("No replications to sync")
        return results

    try:
        # Perform bulk reconciliation with a single API call
        reconcile_result = await rest_bulk_reconcile_async(
            nb,
            "/api/plugins/proxbox/replications/",
            payloads=all_payloads,
            lookup_fields=["replication_id"],
            schema=dict,
            current_normalizer=lambda record: {
                "replication_id": record.get("replication_id"),
                "guest": record.get("guest"),
                "target": record.get("target"),
                "job_type": record.get("job_type"),
                "schedule": record.get("schedule"),
                "rate": record.get("rate"),
                "comment": record.get("comment"),
                "disable": record.get("disable"),
                "source": record.get("source"),
                "jobnum": record.get("jobnum"),
                "remove_job": record.get("remove_job"),
                "virtual_machine": record.get("virtual_machine"),
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
