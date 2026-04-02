"""VM resource filtering utilities - extracted from sync_vm.py."""

from __future__ import annotations

from proxbox_api.dependencies import NetBoxSessionDep
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_first_async
from proxbox_api.services.sync.vm_helpers import parse_key_value_string


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

    Extracts net0, net1, net2, etc. from config and parses key=value pairs.

    Args:
        vm_config: VM configuration dict from Proxmox

    Returns:
        List of parsed network configs
    """
    networks: list[dict[str, dict[str, str]]] = []
    network_id = 0
    while True:
        network_name = f"net{network_id}"
        network_info = vm_config.get(network_name)
        if network_info is None:
            break
        try:
            network_dict = parse_key_value_string(network_info)
            if not network_dict:
                logger.debug(
                    "Skipping non-string or empty network config %s during parse: %r",
                    network_name,
                    type(network_info).__name__,
                )
                network_id += 1
                continue
            networks.append({network_name: network_dict})
        except (ValueError, IndexError) as e:
            logger.warning("Failed to parse network config %s: %s", network_name, e)
        network_id += 1

    return networks


def get_interface_name_from_config_and_agent(
    config_interface_name: str,
    config_dict: dict[str, object],
    guest_agent_interfaces: list[dict[str, object]],
    use_guest_agent_name: bool = True,
) -> str:
    """Determine final interface name from config and guest agent data.

    Prefers guest agent name if available and enabled.
    Falls back to config name otherwise.

    Args:
        config_interface_name: Interface name from Proxmox config
        config_dict: Network config dictionary
        guest_agent_interfaces: List of interfaces from guest agent
        use_guest_agent_name: Whether to use guest agent names

    Returns:
        Resolved interface name
    """
    from proxbox_api.services.sync.vm_helpers import normalized_mac

    if not use_guest_agent_name:
        return config_interface_name

    # Try to match by MAC address first
    interface_mac = config_dict.get("virtio") or config_dict.get("hwaddr")
    if interface_mac:
        for guest_iface in guest_agent_interfaces:
            if normalized_mac(guest_iface.get("mac_address")) == normalized_mac(interface_mac):
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
