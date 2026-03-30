"""VM backup discovery, batch processing, and sync routes."""

# FastAPI Imports
import asyncio
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import (
    NetBoxSessionDep,  # NetBox Session
    ProxboxTagDep,  # Proxbox Tag
)
from proxbox_api.exception import ProxboxException  # Proxbox Exception
from proxbox_api.logger import logger

# NetBox compatibility wrappers
from proxbox_api.netbox_rest import (
    rest_create,
    rest_create_async,
    rest_list,
    rest_list_async,
    rest_reconcile_async,
)
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxBackupSyncState,
)
from proxbox_api.routes.proxmox import (
    get_proxmox_node_storage_content,  # Get VM Config
)  # Get Proxmox Node Storage Content
from proxbox_api.routes.proxmox.cluster import (
    ClusterStatusDep,
)  # Cluster Status and Resources
from proxbox_api.session.proxmox import ProxmoxSessionsDep  # Sessions
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_event

router = APIRouter()


def _volids_from_proxmox_storage_backup_items(items: list[dict]) -> set[str]:
    """Collect Proxmox volume IDs for backup content rows (volid / NetBox volume_id)."""
    out: set[str] = set()
    for item in items:
        if item.get("content") != "backup":
            continue
        vid = item.get("volid")
        if isinstance(vid, str) and vid:
            out.add(vid)
    return out


async def create_netbox_backups(backup, netbox_session: NetBoxSessionDep):
    nb = netbox_session
    vmid_log: str | int | None = None
    try:
        if not isinstance(backup, dict):
            return None
        # Get the virtual machine on NetBox by the VM ID.
        vmid = backup.get("vmid", None)
        vmid_log = vmid
        if not vmid:
            return None

        # Get the virtual machine on NetBox by the VM ID using custom field filter
        vms = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"cf_proxmox_vm_id": int(vmid)},
        )
        virtual_machine = vms[0] if vms else None

        if not virtual_machine:
            return None

        # Process verification data
        verification = backup.get("verification", {})
        verification_state = verification.get("state")
        verification_upid = verification.get("upid")

        # Process storage and volume data
        volume_id = backup.get("volid", None)
        storage_name = volume_id.split(":")[0] if volume_id else None

        # Process creation time
        creation_time = None
        ctime = backup.get("ctime", None)
        if ctime:
            creation_time = datetime.fromtimestamp(ctime).isoformat()

        backup_payload = {
            "storage": storage_name,
            "virtual_machine": virtual_machine.get("id"),
            "subtype": backup.get("subtype"),
            "creation_time": creation_time,
            "size": backup.get("size"),
            "verification_state": verification_state,
            "verification_upid": verification_upid,
            "volume_id": volume_id,
            "notes": backup.get("notes"),
            "vmid": vmid,
            "format": backup.get("format"),
        }

        netbox_backup = await rest_reconcile_async(
            nb,
            "/api/plugins/proxbox/backups/",
            lookup={"volume_id": volume_id},
            payload=backup_payload,
            schema=NetBoxBackupSyncState,
            current_normalizer=lambda record: {
                "storage": record.get("storage"),
                "virtual_machine": record.get("virtual_machine"),
                "subtype": record.get("subtype"),
                "creation_time": record.get("creation_time"),
                "size": record.get("size"),
                "verification_state": record.get("verification_state"),
                "verification_upid": record.get("verification_upid"),
                "volume_id": record.get("volume_id"),
                "notes": record.get("notes"),
                "vmid": record.get("vmid"),
                "format": record.get("format"),
            },
        )

        # Create a journal entry for the backup
        await rest_create_async(
            nb,
            "/api/extras/journal-entries/",
            {
                "assigned_object_type": "netbox_proxbox.vmbackup",
                "assigned_object_id": netbox_backup.id,
                "kind": "info",
                "comments": f"Backup created for VM {vmid} in storage {storage_name}",
            },
        )

        return netbox_backup

    except Exception as error:
        logger.warning("Error creating NetBox backup for VM %s: %s", vmid_log, error)
        return None


async def process_backups_batch(backup_tasks: list, batch_size: int = 10) -> tuple[list, int]:
    """
    Process a list of backup tasks in batches to avoid overwhelming the API.

    Returns:
        (successful_reconcile_results, failure_count) where failures are exceptions from gather.
    """
    results: list = []
    failures = 0
    for i in range(0, len(backup_tasks), batch_size):
        batch = backup_tasks[i : i + batch_size]
        batch_results = await asyncio.gather(*batch, return_exceptions=True)
        for r in batch_results:
            if isinstance(r, Exception):
                failures += 1
            elif r is not None:
                results.append(r)
    return results, failures


async def get_node_backups(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    node: str,
    storage: str,
    netbox_session: NetBoxSessionDep,
    vmid: str | None = None,
) -> tuple[list, set[str]]:
    nb = netbox_session
    """
    Get backups for a specific node and storage.

    Returns:
        (async tasks for NetBox reconcile, set of Proxmox volid strings seen on storage)
    """
    for proxmox, cluster in zip(pxs, cluster_status):
        if cluster and cluster.node_list:
            for cluster_node in cluster.node_list:
                if cluster_node.name == node:
                    try:
                        backups = await get_proxmox_node_storage_content(
                            pxs=pxs,
                            cluster_status=cluster_status,
                            node=node,
                            storage=storage,
                            vmid=vmid,
                            content="backup",
                        )

                        volids = _volids_from_proxmox_storage_backup_items(backups)
                        tasks = [
                            create_netbox_backups(backup, nb)
                            for backup in backups
                            if backup.get("content") == "backup"
                        ]
                        return tasks, volids
                    except Exception as error:
                        logger.warning("Error getting backups for node %s: %s", node, error)
                        continue
    return [], set()


@router.get("/backups/create")
async def create_virtual_machine_backups(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    node: Annotated[
        str,
        Query(
            title="Node",
            description="The name of the node to retrieve the storage content for.",
        ),
    ],
    storage: Annotated[
        str,
        Query(
            title="Storage",
            description="The name of the storage to retrieve the content for.",
        ),
    ],
    vmid: Annotated[
        str | None,
        Query(title="VM ID", description="The ID of the VM to retrieve the content for."),
    ] = None,
):
    backup_tasks, _volids = await get_node_backups(
        pxs, cluster_status, node, storage, netbox_session=netbox_session, vmid=vmid
    )
    if not backup_tasks:
        raise ProxboxException(message="Node or Storage not found.")

    reconciled, _failures = await process_backups_batch(backup_tasks)
    return reconciled


async def _create_all_virtual_machine_backups(
    netbox_session,
    pxs,
    cluster_status,
    tag,
    delete_nonexistent_backup=False,
    websocket=None,
    use_websocket=False,
):
    """Internal function that handles backup sync with optional websocket support."""
    nb = netbox_session
    start_time = datetime.now()
    sync_process = None
    results = []
    journal_messages = []  # Store all journal messages
    failure_count = 0
    deleted_count = 0  # Track number of deleted backups
    backup_sync_ok = False

    try:
        # Create sync process
        sync_process = rest_create(
            nb,
            "/api/plugins/proxbox/sync-processes/",
            {
                "name": f"sync-virtual-machines-backups-{start_time}",
                "sync_type": "vm-backups",
                "status": "not-started",
                "started_at": str(start_time),
                "completed_at": None,
                "runtime": None,
                "tags": [tag.id],
            },
        )

        journal_messages.append("## Backup Sync Process Started")
        journal_messages.append(f"- **Start Time**: {start_time}")
        journal_messages.append("- **Status**: Initializing")

    except Exception as error:
        error_msg = f"Error creating sync process: {str(error)}"
        journal_messages.append(f"### ❌ Error\n{error_msg}")
        raise ProxboxException(message=error_msg)

    try:
        sync_process.status = "syncing"
        sync_process.save()

        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "backups",
                    "status": "started",
                    "message": "Starting backup synchronization.",
                }
            )

        journal_messages.append("\n## Backup Discovery")
        all_backup_tasks = []
        proxmox_backups = set()  # Store all Proxmox backup identifiers

        # Process each Proxmox cluster
        for proxmox, cluster in zip(pxs, cluster_status):
            # Get all storage names that have 'backup' in the content
            storage_list = [
                {
                    "storage": storage_dict.get("storage"),
                    "nodes": storage_dict.get("nodes", "all"),
                }
                for storage_dict in proxmox.session.storage.get()
                if "backup" in storage_dict.get("content")
            ]

            journal_messages.append(f"\n### Processing Cluster: {cluster.name}")
            journal_messages.append(f"- Found {len(storage_list)} backup storages")

            # Process each cluster node
            if cluster and cluster.node_list:
                for cluster_node in cluster.node_list:
                    # Process each storage
                    for storage in storage_list:
                        if storage.get("nodes") == "all" or cluster_node.name in storage.get(
                            "nodes", []
                        ):
                            try:
                                node_backup_tasks, node_volids = await get_node_backups(
                                    pxs=pxs,
                                    cluster_status=cluster_status,
                                    node=cluster_node.name,
                                    storage=storage.get("storage"),
                                    netbox_session=nb,
                                )
                                all_backup_tasks.extend(node_backup_tasks)
                                proxmox_backups.update(node_volids)

                                journal_messages.append(
                                    f"- Node `{cluster_node.name}` in storage `{storage.get('storage')}`: Found {len(node_backup_tasks)} backups"
                                )

                            except Exception as error:
                                error_msg = f"Error processing backups for node {cluster_node.name} and storage {storage.get('storage')}: {str(error)}"
                                journal_messages.append(f"  - ❌ {error_msg}")
                                continue

        if not all_backup_tasks:
            error_msg = "No backups found to process"
            journal_messages.append(f"\n### ⚠️ Warning\n{error_msg}")
            if use_websocket and websocket:
                await websocket.send_json(
                    {
                        "step": "backups",
                        "status": "warning",
                        "message": error_msg,
                    }
                )
            raise ProxboxException(message=error_msg)

        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "backups",
                    "status": "discovered",
                    "message": f"Found {len(all_backup_tasks)} backups to process.",
                    "count": len(all_backup_tasks),
                }
            )

        journal_messages.append("\n## Backup Processing")
        journal_messages.append(f"- Total backups to process: {len(all_backup_tasks)}")

        # Process all backups in batches
        results, failure_count = await process_backups_batch(all_backup_tasks)

        journal_messages.append(
            f"- Reconciled {len(results)} backup record(s) in NetBox (create/update via API)"
        )
        if failure_count:
            journal_messages.append(f"- Tasks that raised errors: {failure_count}")

        # Handle deletion of nonexistent backups if requested
        if delete_nonexistent_backup:
            journal_messages.append("\n## Deleting Nonexistent Backups")
            try:
                # Get all backups from NetBox
                netbox_backups = rest_list(nb, "/api/plugins/proxbox/backups/")
                skipped_no_volid = 0

                for backup in netbox_backups:
                    vid = backup.volume_id
                    if not vid:
                        skipped_no_volid += 1
                        continue
                    if vid not in proxmox_backups:
                        try:
                            # Delete the backup
                            backup.delete()
                            deleted_count += 1
                            journal_messages.append(
                                f"- Deleted backup for VM ID {backup.vmid} in storage {backup.storage} (volume: {backup.volume_id})"
                            )
                        except Exception as error:
                            journal_messages.append(
                                f"- ❌ Failed to delete backup for VM ID {backup.vmid}: {str(error)}"
                            )

                if skipped_no_volid:
                    journal_messages.append(
                        f"- Skipped {skipped_no_volid} NetBox backup(s) with empty volume_id "
                        "(cannot match Proxmox)"
                    )

                if deleted_count > 0:
                    journal_messages.append(f"\nTotal backups deleted: {deleted_count}")
                else:
                    journal_messages.append("\nNo backups needed to be deleted")

            except Exception as error:
                error_msg = f"Error during backup deletion: {str(error)}"
                journal_messages.append(f"\n### ❌ Error\n{error_msg}")
                # Don't raise the exception as this is not critical for the sync process

        backup_sync_ok = True

    except Exception as error:
        error_msg = f"Error during backup sync: {str(error)}"
        journal_messages.append(f"\n### ❌ Error\n{error_msg}")
        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "backups",
                    "status": "failed",
                    "message": error_msg,
                    "error": str(error),
                }
            )
        raise ProxboxException(message=error_msg)

    finally:
        # Always update sync process status
        if sync_process:
            end_time = datetime.now()
            sync_process.completed_at = str(end_time)
            sync_process.runtime = float((end_time - start_time).total_seconds())
            if backup_sync_ok:
                sync_process.status = "completed"
            elif sync_process.status == "syncing":
                sync_process.status = "failed"
            sync_process.save()

            # Add final summary
            journal_messages.append("\n## Process Summary")
            journal_messages.append(f"- **Status**: {sync_process.status}")
            journal_messages.append(f"- **Runtime**: {sync_process.runtime} seconds")
            journal_messages.append(f"- **End Time**: {end_time}")
            journal_messages.append(
                f"- **Backup tasks OK**: {len(results)} reconciled, {failure_count} task error(s)"
            )
            if delete_nonexistent_backup:
                journal_messages.append(f"- **Backups Deleted**: {deleted_count}")

            journal_entry = await rest_create_async(
                nb,
                "/api/extras/journal-entries/",
                {
                    "assigned_object_type": "netbox_proxbox.syncprocess",
                    "assigned_object_id": sync_process.id,
                    "kind": "info",
                    "comments": "\n".join(journal_messages),
                },
            )

            if not journal_entry:
                logger.warning("Journal entry creation returned None")

            if use_websocket and websocket and backup_sync_ok:
                await websocket.send_json(
                    {
                        "step": "backups",
                        "status": "completed",
                        "message": (
                            f"Backup sync completed. {len(results)} reconciled, "
                            f"{failure_count} task error(s), {deleted_count} deleted."
                        ),
                        "result": {
                            "reconciled": len(results),
                            "failed_tasks": failure_count,
                            "deleted": deleted_count,
                        },
                    }
                )

    logger.info("Syncing backups finished")
    return results


@router.get("/backups/all/create")
async def create_all_virtual_machine_backups(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    tag: ProxboxTagDep,
    delete_nonexistent_backup: Annotated[
        bool,
        Query(
            title="Delete Nonexistent Backup",
            description="If true, deletes backups that exist in NetBox but not in Proxmox.",
        ),
    ] = False,
):
    return await _create_all_virtual_machine_backups(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        tag=tag,
        delete_nonexistent_backup=delete_nonexistent_backup,
    )


@router.get("/backups/all/create/stream", response_model=None)
async def create_all_virtual_machine_backups_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    tag: ProxboxTagDep,
    delete_nonexistent_backup: Annotated[
        bool,
        Query(
            title="Delete Nonexistent Backup",
            description="If true, deletes backups that exist in NetBox but not in Proxmox.",
        ),
    ] = False,
):
    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await _create_all_virtual_machine_backups(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    tag=tag,
                    delete_nonexistent_backup=delete_nonexistent_backup,
                    websocket=bridge,
                    use_websocket=True,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        try:
            yield sse_event(
                "step",
                {
                    "step": "backups",
                    "status": "started",
                    "message": "Starting backup synchronization.",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame
            result = await sync_task
            yield sse_event(
                "step",
                {
                    "step": "backups",
                    "status": "completed",
                    "message": "Backup synchronization finished.",
                    "result": {"count": len(result) if result else 0},
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Backup sync completed.",
                    "result": {"count": len(result) if result else 0},
                },
            )
        except Exception as error:
            yield sse_event(
                "error",
                {
                    "step": "backups",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Backup sync failed.",
                    "errors": [{"detail": str(error)}],
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
