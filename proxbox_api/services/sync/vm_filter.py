"""VM resource filtering utilities - extracted from sync_vm.py."""

from __future__ import annotations

from proxbox_api.dependencies import NetBoxSessionDep
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_first_async
from proxbox_api.services.sync.vm_helpers import parse_proxmox_net_configs


async def filter_cluster_resources_by_netbox_vm_ids(  # noqa: C901
    netbox_session: NetBoxSessionDep,
    cluster_resources: list[dict[str, object]],
    netbox_vm_ids: list[int],
) -> list[dict[str, object]]:
    """Filter cluster resources to only include VMs matching the given NetBox VM IDs.

    Args:
        netbox_session: NetBox session
        cluster_resources: List of cluster resources
        netbox_vm_ids: NetBox VM IDs to filter by

    Returns:
        Filtered cluster resources
    """
    if not netbox_vm_ids:
        return cluster_resources

    id_to_vm: dict[int, dict[str, object]] = {}
    for vm_id in netbox_vm_ids:
        id_to_vm[vm_id] = {"id": vm_id, "name": None, "cluster": None, "cf_proxmox_vm_id": None}

    # Fetch VM details from NetBox
    try:
        vms = await rest_first_async(
            netbox_session,
            "/api/virtualization/virtual-machines/",
            query={"id": ",".join(str(vid) for vid in netbox_vm_ids)},
        )
        if vms and isinstance(vms, list):
            for vm in vms:
                if not isinstance(vm, dict):
                    continue
                vm_id = vm.get("id")
                if vm_id is not None:
                    id_to_vm[vm_id] = vm
    except Exception as e:
        logger.debug("Failed to fetch VM details from NetBox: %s", e)

    # Extract target identifiers from NetBox VMs
    target_proxmox_vm_ids: set[int] = set()
    target_vm_names: set[str] = set()
    target_cluster_ids: set[int] = set()

    for vm in id_to_vm.values():
        cf = vm.get("custom_fields", {}) or {}
        raw_vmid = cf.get("proxmox_vm_id")
        if raw_vmid is not None and str(raw_vmid).strip().isdigit():
            target_proxmox_vm_ids.add(int(str(raw_vmid).strip()))
        vm_name = str(vm.get("name", "")).strip()
        if vm_name:
            target_vm_names.add(vm_name.lower())
        cluster = vm.get("cluster")
        if isinstance(cluster, dict):
            cluster_id = cluster.get("id")
            if isinstance(cluster_id, int):
                target_cluster_ids.add(cluster_id)

    # Filter resources by target identifiers
    filtered: list[dict[str, object]] = []
    for cluster in cluster_resources:
        if not isinstance(cluster, dict):
            continue
        for cluster_key, resources in cluster.items():
            if not isinstance(resources, list):
                continue
            selected = []
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                if resource.get("type") not in ("qemu", "lxc"):
                    continue
                res_vmid = resource.get("vmid")
                if res_vmid is not None and int(res_vmid) in target_proxmox_vm_ids:
                    selected.append(resource)
                    continue
                res_name = str(resource.get("name", "")).strip().lower()
                if res_name in target_vm_names:
                    selected.append(resource)
                    continue
            if selected:
                filtered.append({cluster_key: selected})

    return filtered


def parse_network_config(vm_config: dict[str, object]) -> list[dict[str, dict[str, str]]]:
    """Parse Proxmox VM network configuration into list of network dicts.

    Extracts exact net<N> entries from config and parses key=value pairs.

    Args:
        vm_config: VM configuration dict from Proxmox

    Returns:
        List of parsed network configs
    """
    return parse_proxmox_net_configs(vm_config)


def get_interface_name_from_config_and_agent(
    config_interface_name: str,
    config_dict: dict[str, object],
    guest_agent_interfaces: list[dict[str, object]],
    use_guest_agent_name: bool = True,
    vm_interface_sync_strategy: object = "guest_os_model",
) -> str:
    """Determine final interface name from config and guest agent data.

    The current default keeps the Proxmox config name for the core
    virtualization.VMInterface. The deprecated ``legacy_rename`` strategy
    preserves the old behavior and prefers guest-agent names when enabled.

    Args:
        config_interface_name: Interface name from Proxmox config
        config_dict: Network config dictionary
        guest_agent_interfaces: List of interfaces from guest agent
        use_guest_agent_name: Whether to use guest agent names
        vm_interface_sync_strategy: guest_os_model (default) or legacy_rename

    Returns:
        Resolved interface name
    """
    from proxbox_api.services.sync.guest_vm_interface import (
        should_use_guest_agent_core_interface_name,
    )
    from proxbox_api.services.sync.vm_helpers import (
        build_guest_mac_index,
        merged_guest_iface_from_mac_index,
    )

    if not should_use_guest_agent_core_interface_name(
        use_guest_agent_name,
        vm_interface_sync_strategy,
    ):
        return config_interface_name

    # Try to match by MAC address first
    interface_mac = config_dict.get("virtio") or config_dict.get("hwaddr")
    if interface_mac:
        guest_iface = merged_guest_iface_from_mac_index(
            build_guest_mac_index(guest_agent_interfaces),
            interface_mac,
        )
        if guest_iface:
            guest_name = str(guest_iface.get("name") or "").strip()
            if guest_name:
                return guest_name

    # Try to match by name
    for guest_iface in guest_agent_interfaces:
        if str(guest_iface.get("name", "")).strip().lower() == config_interface_name.lower():
            guest_name = str(guest_iface.get("name") or "").strip()
            if guest_name:
                return guest_name

    return config_interface_name
