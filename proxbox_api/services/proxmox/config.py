"""VM config resolution helpers used by routes and sync services."""

from __future__ import annotations

from typing import Iterable

from proxmox_sdk.sdk.exceptions import ResourceException

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.schemas.proxmox import ClusterStatusSchemaList
from proxbox_api.services.proxmox_helpers import get_vm_config as get_typed_vm_config


async def resolve_vm_config(
    *,
    pxs: Iterable[object],
    cluster_status: ClusterStatusSchemaList,
    node: str,
    vm_type: str,
    vmid: int,
) -> dict[str, object]:
    """Resolve a VM config across all Proxmox sessions and return the dumped payload.

    Raises:
        ProxboxException: invalid VM type, no matching node, or downstream Proxmox failure.
    """
    if vm_type not in ("qemu", "lxc"):
        raise ProxboxException(
            message="Invalid VM Type. Use 'qemu' or 'lxc'.",
            http_status_code=400,
        )

    config = None
    try:
        for px, cluster in zip(pxs, cluster_status):
            try:
                node_list = cluster.node_list or []
                for cluster_node in node_list:
                    if str(node) == str(cluster_node.name):
                        config = await get_typed_vm_config(
                            px, node=node, vm_type=vm_type, vmid=vmid
                        )
                        if config:
                            return config.model_dump(
                                mode="python",
                                by_alias=True,
                                exclude_none=True,
                            )
            except ResourceException as error:
                raise ProxboxException(
                    message="Error getting VM Config",
                    python_exception=f"Error: {error!s}",
                )

        if config is None:
            raise ProxboxException(
                message="VM Config not found.",
                detail="VM Config not found. Check if the 'node', 'type', and 'vmid' are correct.",
            )
    except ProxboxException:
        raise
    except Exception as error:
        logger.exception(
            "Unhandled error while getting VM config for node=%s type=%s vmid=%s",
            node,
            vm_type,
            vmid,
        )
        raise ProxboxException(
            message="Unknown error getting VM Config. Search parameters probably wrong.",
            detail="Check if the node, type, and vmid are correct.",
            python_exception=str(error),
        )

    return {}
