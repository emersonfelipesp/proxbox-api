"""Typed helpers for direct proxmoxer calls validated through generated models."""

from __future__ import annotations

from proxbox_api.exception import ProxboxException
from proxbox_api.generated.proxmox.latest import pydantic_models as generated_models
from proxbox_api.logger import logger
from proxbox_api.session.proxmox import ProxmoxSession


def _model_dump(model: object) -> dict[str, object]:
    return model.model_dump(mode="python", by_alias=True, exclude_none=True)


def _wrap_backend_call(message: str, operation):
    try:
        return operation()
    except ProxboxException:
        raise
    except Exception as error:
        raise ProxboxException(message=message, python_exception=str(error))


def get_cluster_status(
    session: ProxmoxSession,
) -> list[generated_models.GetClusterStatusResponseItem]:
    return _wrap_backend_call(
        "Error fetching Proxmox cluster status",
        lambda: (
            generated_models.GetClusterStatusResponse.model_validate(
                session.session("cluster/status").get()
            ).root
        ),
    )


def get_cluster_resources(
    session: ProxmoxSession,
    resource_type: str | None = None,
) -> list[generated_models.GetClusterResourcesResponseItem]:
    return _wrap_backend_call(
        "Error fetching Proxmox cluster resources",
        lambda: (
            generated_models.GetClusterResourcesResponse.model_validate(
                session.session("cluster/resources").get(type=resource_type)
                if resource_type
                else session.session("cluster/resources").get()
            ).root
        ),
    )


def get_cluster_replication(
    session: ProxmoxSession,
) -> list[dict[str, object]]:
    """Get cluster replication jobs from Proxmox."""
    try:
        result = session.session("cluster/replication").get()
        return result if isinstance(result, list) else []
    except Exception:
        return []


def get_vm_config(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> (
    generated_models.GetNodesNodeQemuVmidConfigResponse
    | generated_models.GetNodesNodeLxcVmidConfigResponse
):
    def _fetch_config():
        if vm_type == "qemu":
            payload = session.session.nodes(node).qemu(vmid).config.get()
            return generated_models.GetNodesNodeQemuVmidConfigResponse.model_validate(payload)
        if vm_type == "lxc":
            payload = session.session.nodes(node).lxc(vmid).config.get()
            return generated_models.GetNodesNodeLxcVmidConfigResponse.model_validate(payload)
        raise ValueError(f"Unsupported VM type: {vm_type}")

    return _wrap_backend_call("Error fetching Proxmox VM config", _fetch_config)


def _normalize_guest_agent_interfaces(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, dict):
        raw_interfaces = payload.get("result") or payload.get("interfaces") or []
    elif isinstance(payload, list):
        raw_interfaces = payload
    else:
        raw_interfaces = []

    normalized: list[dict[str, object]] = []
    for item in raw_interfaces:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        addresses: list[dict[str, object]] = []
        for addr in item.get("ip-addresses") or item.get("ip_addresses") or []:
            if not isinstance(addr, dict):
                continue
            ip_address = addr.get("ip-address") or addr.get("ip_address")
            if not ip_address:
                continue
            addresses.append(
                {
                    "ip_address": str(ip_address),
                    "prefix": addr.get("prefix"),
                    "ip_address_type": addr.get("ip-address-type") or addr.get("ip_address_type"),
                }
            )
        normalized.append(
            {
                "name": str(name),
                "mac_address": item.get("hardware-address") or item.get("hardware_address"),
                "ip_addresses": addresses,
            }
        )
    return normalized


def get_qemu_guest_agent_network_interfaces(
    session: ProxmoxSession,
    node: str,
    vmid: int,
) -> list[dict[str, object]]:
    """Return normalized guest-agent interfaces or [] when unavailable."""

    try:
        try:
            payload = session.session.nodes(node).qemu(vmid).agent("network-get-interfaces").get()
        except Exception as error:
            logger.debug(
                "Primary guest-agent interfaces call failed for node=%s vmid=%s: %s",
                node,
                vmid,
                error,
            )
            payload = (
                session.session.nodes(node).qemu(vmid).agent.get(command="network-get-interfaces")
            )
        return _normalize_guest_agent_interfaces(payload)
    except Exception as error:
        logger.warning(
            "Unable to fetch guest-agent interfaces for node=%s vmid=%s: %s",
            node,
            vmid,
            error,
        )
        return []


def get_storage_list(
    session: ProxmoxSession,
) -> list[generated_models.GetStorageResponseItem]:
    return _wrap_backend_call(
        "Error fetching Proxmox storage list",
        lambda: (
            generated_models.GetStorageResponse.model_validate(session.session.storage.get()).root
        ),
    )


def get_storage_config(
    session: ProxmoxSession,
    storage_id: str,
) -> dict[str, object]:
    """Fetch full storage configuration from Proxmox.

    The /storage endpoint only returns storage IDs. This helper fetches
    the full configuration including type, content, path, nodes, shared, etc.
    """
    return _wrap_backend_call(
        f"Error fetching Proxmox storage config for {storage_id}",
        lambda: _model_dump(
            generated_models.GetStorageStorageResponse.model_validate(
                session.session.storage(storage_id).get()
            )
        ),
    )


def get_node_storage_content(
    session: ProxmoxSession,
    node: str,
    storage: str,
    **kwargs: object,
) -> list[generated_models.GetNodesNodeStorageStorageContentResponseItem]:
    params = {key: value for key, value in kwargs.items() if value is not None}
    return _wrap_backend_call(
        "Error fetching Proxmox node storage content",
        lambda: (
            generated_models.GetNodesNodeStorageStorageContentResponse.model_validate(
                session.session.nodes(node).storage(storage).content.get(**params)
            ).root
        ),
    )


def get_node_tasks(
    session: ProxmoxSession,
    node: str,
    *,
    vmid: int | None = None,
    source: str | None = "archive",
    statusfilter: str | None = None,
    typefilter: str | None = None,
    until: int | None = None,
    userfilter: str | None = None,
) -> list[generated_models.GetNodesNodeTasksResponseItem]:
    params = {
        "vmid": vmid,
        "source": source,
        "statusfilter": statusfilter,
        "typefilter": typefilter,
        "until": until,
        "userfilter": userfilter,
    }
    filtered = {key: value for key, value in params.items() if value is not None}
    return _wrap_backend_call(
        "Error fetching Proxmox node tasks",
        lambda: (
            generated_models.GetNodesNodeTasksResponse.model_validate(
                session.session.nodes(node).tasks.get(**filtered)
            ).root
        ),
    )


def get_node_task_status(
    session: ProxmoxSession,
    node: str,
    upid: str,
) -> generated_models.GetNodesNodeTasksUpidStatusResponse:
    return _wrap_backend_call(
        "Error fetching Proxmox task status",
        lambda: generated_models.GetNodesNodeTasksUpidStatusResponse.model_validate(
            session.session.nodes(node).tasks(upid).status.get()
        ),
    )


def dump_models(items: list[object]) -> list[dict[str, object]]:
    return [_model_dump(item) for item in items]


def get_vm_snapshots(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> list[dict[str, object]]:
    """
    Get snapshots for a specific VM from Proxmox.

    Args:
        session: Proxmox session
        node: Proxmox node name
        vm_type: 'qemu' or 'lxc'
        vmid: Proxmox VM ID

    Returns:
        List of snapshot dictionaries
    """

    def _fetch_snapshots():
        if vm_type == "qemu":
            payload = session.session.nodes(node).qemu(vmid).snapshot.get()
        elif vm_type == "lxc":
            payload = session.session.nodes(node).lxc(vmid).snapshot.get()
        else:
            raise ValueError(f"Unsupported VM type: {vm_type}")
        return payload

    try:
        result = _wrap_backend_call(
            f"Error fetching snapshots for VM {vmid} on node {node}",
            _fetch_snapshots,
        )
        return result if isinstance(result, list) else []
    except Exception as error:
        logger.warning(
            "Error fetching snapshots for vmid=%s node=%s type=%s: %s",
            vmid,
            node,
            vm_type,
            error,
        )
        return []


def get_cluster_snapshots_for_vm(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> list[dict[str, object]]:
    """
    Get all snapshots for a VM across all nodes in the cluster.
    Uses the VM's configured nodes if available.

    Args:
        session: Proxmox session
        node: Proxmox node name
        vm_type: 'qemu' or 'lxc'
        vmid: Proxmox VM ID

    Returns:
        List of snapshot dictionaries from all accessible nodes
    """
    all_snapshots = []

    try:
        snapshots = get_vm_snapshots(session, node, vm_type, vmid)
        all_snapshots.extend(snapshots)
    except Exception as error:
        logger.warning(
            "Error aggregating cluster snapshots for vmid=%s node=%s type=%s: %s",
            vmid,
            node,
            vm_type,
            error,
        )

    return all_snapshots


def get_node_status_individual(
    session: ProxmoxSession,
    node: str,
) -> dict[str, object]:
    """Get a single node's status from cluster status.

    Args:
        session: Proxmox session.
        node: Node name to filter by.

    Returns:
        Node status dict or empty dict if not found.
    """
    try:
        status_list = get_cluster_status(session)
        for item in status_list:
            if str(item.get("node", "")) == node or str(item.get("name", "")) == node:
                return _model_dump(item)
    except Exception as error:
        logger.warning(
            "Error fetching node status for node=%s: %s",
            node,
            error,
        )
    return {}


def get_storage_config_individual(
    session: ProxmoxSession,
    storage_id: str,
) -> dict[str, object]:
    """Get storage configuration for a specific storage.

    Args:
        session: Proxmox session.
        storage_id: Storage identifier.

    Returns:
        Storage config dict.
    """
    return get_storage_config(session, storage_id)


def get_vm_config_individual(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> dict[str, object]:
    """Get VM configuration for a specific VM.

    Args:
        session: Proxmox session.
        node: Node name.
        vm_type: 'qemu' or 'lxc'.
        vmid: VM ID.

    Returns:
        VM config dictionary.
    """
    config = get_vm_config(session, node, vm_type, vmid)
    return _model_dump(config)


def get_vm_snapshots_individual(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> list[dict[str, object]]:
    """Get snapshots for a specific VM.

    Args:
        session: Proxmox session.
        node: Node name.
        vm_type: 'qemu' or 'lxc'.
        vmid: VM ID.

    Returns:
        List of snapshot dictionaries.
    """
    return get_vm_snapshots(session, node, vm_type, vmid)


def get_vm_backups_individual(
    session: ProxmoxSession,
    node: str,
    storage: str,
    vmid: int,
) -> list[dict[str, object]]:
    """Get backups for a specific VM from storage content.

    Args:
        session: Proxmox session.
        node: Node name.
        storage: Storage name.
        vmid: VM ID to filter backups.

    Returns:
        List of backup dictionaries matching the VM.
    """
    try:
        content = get_node_storage_content(session, node, storage, vmid=str(vmid), content="backup")
        backups = []
        for item in content:
            item_dict = _model_dump(item)
            item_vmid = item_dict.get("vmid")
            if item_vmid is not None and int(item_vmid) == vmid:
                backups.append(item_dict)
        return backups
    except Exception as error:
        logger.warning(
            "Error fetching backups for vmid=%s node=%s storage=%s: %s",
            vmid,
            node,
            storage,
            error,
        )
        return []


def get_vm_tasks_individual(
    session: ProxmoxSession,
    node: str,
    vmid: int | None = None,
    source: str = "archive",
) -> list[dict[str, object]]:
    """Get tasks for a specific VM.

    Args:
        session: Proxmox session.
        node: Node name.
        vmid: Optional VM ID to filter tasks.
        source: Task source filter ('archive' or 'active').

    Returns:
        List of task dictionaries.
    """
    try:
        tasks = get_node_tasks(session, node, vmid=vmid, source=source)
        task_dicts = [_model_dump(t) for t in tasks]
        if vmid is not None:
            filtered = []
            for task in task_dicts:
                task_vmid = task.get("vmid")
                if task_vmid is not None and int(task_vmid) == vmid:
                    filtered.append(task)
            return filtered
        return task_dicts
    except Exception as error:
        logger.warning(
            "Error fetching tasks for node=%s vmid=%s: %s",
            node,
            vmid,
            error,
        )
        return []


def get_vm_resource_individual(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> dict[str, object]:
    """Get a single VM resource from cluster resources filtered by node and vmid.

    Args:
        session: Proxmox session.
        node: Node name.
        vm_type: VM type ('qemu' or 'lxc').
        vmid: VM ID.

    Returns:
        VM resource dict or empty dict if not found.
    """
    try:
        resources = get_cluster_resources(session)
        for resource in resources:
            resource_dict = _model_dump(resource)
            res_type = resource_dict.get("type", "")
            res_node = resource_dict.get("node", "")
            res_vmid = resource_dict.get("vmid")
            if (
                res_type == vm_type
                and res_node == node
                and res_vmid is not None
                and int(res_vmid) == vmid
            ):
                return resource_dict
    except Exception as error:
        logger.warning(
            "Error fetching VM resource for node=%s type=%s vmid=%s: %s",
            node,
            vm_type,
            vmid,
            error,
        )
    return {}
