"""Replication sync service for syncing replications from Proxmox to NetBox."""

from __future__ import annotations

import asyncio

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.session.proxmox import ProxmoxSessionsDep


async def sync_all_replications(
    netbox_session,
    pxs: ProxmoxSessionsDep,
) -> dict:
    """Sync all replication jobs from Proxmox to NetBox.

    Args:
        netbox_session: NetBox async session.
        pxs: Proxmox sessions dependency.

    Returns:
        Dict with sync results (created, updated, errors counts).
    """
    nb = netbox_session
    results = {"created": 0, "updated": 0, "errors": 0}

    async def sync_replication_for_session(px):
        """Sync replications for a single Proxmox session."""
        session_results = {"created": 0, "updated": 0, "errors": 0}
        try:
            replications = px.session.cluster.replication.get()
        except Exception as e:
            logger.warning("Error fetching replications for %s: %s", px.name, e)
            return session_results

        for rep in replications:
            try:
                guest_vmid = rep.get("guest")
                if not guest_vmid:
                    continue

                vms = await rest_list_async(
                    nb,
                    "/api/virtualization/virtual-machines/",
                    query={"cf_proxmox_vm_id": guest_vmid},
                )
                if not vms:
                    continue

                netbox_vm_id = vms[0].get("id")

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

                existing = await rest_list_async(
                    nb,
                    "/api/plugins/proxbox/replications/",
                    query={"replication_id": rep.get("id")},
                )

                if existing:
                    await rest_reconcile_async(
                        nb,
                        "/api/plugins/proxbox/replications/",
                        lookup={"replication_id": rep.get("id")},
                        payload=replication_payload,
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
                    session_results["updated"] += 1
                else:
                    await rest_reconcile_async(
                        nb,
                        "/api/plugins/proxbox/replications/",
                        lookup={"replication_id": rep.get("id")},
                        payload=replication_payload,
                        schema=dict,
                        current_normalizer=lambda record: {},
                    )
                    session_results["created"] += 1

            except Exception as e:
                logger.warning("Error syncing replication %s: %s", rep.get("id"), e)
                session_results["errors"] += 1

        return session_results

    tasks = [sync_replication_for_session(px) for px in pxs]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in all_results:
        if isinstance(r, dict):
            results["created"] += r.get("created", 0)
            results["updated"] += r.get("updated", 0)
            results["errors"] += r.get("errors", 0)

    logger.info("Replication sync completed: %s", results)
    return results
