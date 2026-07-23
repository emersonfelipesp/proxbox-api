"""VM resource filtering utilities - extracted from sync_vm.py."""

from __future__ import annotations

from dataclasses import dataclass

from proxbox_api.dependencies import NetBoxSessionDep
from proxbox_api.exception import ProxboxException
from proxbox_api.services.custom_fields import custom_fields_enabled, warn_legacy_custom_fields
from proxbox_api.services.sync.sync_state_reader import load_vm_sync_state_identities
from proxbox_api.services.sync.vm_helpers import (
    list_netbox_virtual_machines_by_ids,
    parse_proxmox_net_configs,
    relation_id,
    relation_name,
    to_mapping,
)
from proxbox_api.services.sync.vmid_helpers import (
    extract_proxmox_endpoint_id,
    extract_proxmox_session_endpoint_id,
    extract_proxmox_vm_type,
    extract_proxmox_vmid,
    normalize_positive_int,
)


@dataclass(frozen=True, slots=True)
class _SelectedVMOwner:
    """Collision-safe owner identity for one explicitly selected NetBox VM."""

    netbox_id: int
    endpoint_id: int
    cluster_name: str
    vmid: int
    vm_type: str

    @property
    def resource_key(self) -> tuple[int, str, int, str]:
        return (self.endpoint_id, self.cluster_name, self.vmid, self.vm_type)


def _normalize_cluster_name(value: object) -> str:
    return str(value or "").strip().casefold()


def _field(value: object, name: str) -> object:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _source_endpoint_ids_by_cluster(
    pxs: object,
    cluster_status: object,
) -> dict[str, set[int | None]]:
    """Index every observed cluster alias by its available endpoint owner."""

    sessions = list(pxs or [])
    statuses = list(cluster_status or [])
    endpoint_ids_by_cluster: dict[str, set[int | None]] = {}
    for index, session in enumerate(sessions):
        endpoint_id = extract_proxmox_session_endpoint_id(session)
        status = statuses[index] if index < len(statuses) else None
        for raw_name in (
            _field(status, "name"),
            _field(session, "name"),
            _field(session, "cluster_name"),
        ):
            cluster_name = _normalize_cluster_name(raw_name)
            if cluster_name:
                endpoint_ids_by_cluster.setdefault(cluster_name, set()).add(endpoint_id)
    return endpoint_ids_by_cluster


def _selection_error(detail: str) -> ProxboxException:
    return ProxboxException(
        message="Unable to resolve explicitly selected VM ownership",
        detail=detail,
        http_status_code=502,
    )


def _requested_ids(netbox_vm_ids: list[int]) -> list[int]:
    requested: list[int] = []
    seen: set[int] = set()
    for raw_id in netbox_vm_ids:
        vm_id = normalize_positive_int(raw_id)
        if vm_id is None:
            raise _selection_error(f"Invalid selected NetBox VM id: {raw_id!r}.")
        if vm_id not in seen:
            seen.add(vm_id)
            requested.append(vm_id)
    return requested


def _selected_sidecar_cluster_name(sidecar: dict[str, object]) -> str:
    return _normalize_cluster_name(
        sidecar.get("proxmox_cluster_name") or relation_name(sidecar.get("proxmox_cluster"))
    )


def _overlay_selected_sidecar_identity(
    vm: dict[str, object],
    sidecar: dict[str, object],
    *,
    netbox_id: int,
) -> dict[str, object]:
    """Overlay one verified typed sidecar identity onto a selected VM copy."""

    sidecar_cluster_name = _selected_sidecar_cluster_name(sidecar)
    endpoint_id = normalize_positive_int(sidecar.get("proxmox_endpoint_raw_id"))
    vmid = normalize_positive_int(extract_proxmox_vmid(sidecar))
    vm_type = extract_proxmox_vm_type(sidecar)
    if not sidecar_cluster_name or endpoint_id is None or vmid is None or vm_type is None:
        raise _selection_error(
            f"NetBox VM id {netbox_id} has incomplete or conflicting typed "
            "Proxbox sync-state ownership; endpoint, cluster, positive "
            "VMID, and VM type are required."
        )

    hydrated = dict(vm)
    custom_fields = vm.get("custom_fields")
    hydrated_custom_fields = dict(custom_fields) if isinstance(custom_fields, dict) else {}
    hydrated_custom_fields.update(
        {
            "proxmox_endpoint_id": endpoint_id,
            "proxmox_vm_id": vmid,
            "proxmox_vm_type": vm_type,
        }
    )
    hydrated["custom_fields"] = hydrated_custom_fields
    cluster = vm.get("cluster")
    cluster_id = relation_id(cluster)
    hydrated["cluster"] = {
        **({"id": cluster_id} if cluster_id is not None else {}),
        "name": str(sidecar.get("proxmox_cluster_name") or sidecar_cluster_name).strip(),
    }
    return hydrated


def _hydrate_selected_vm_identity(
    vm: dict[str, object],
    *,
    sidecars_by_vm_id: dict[int, list[dict[str, object]]],
    legacy_fallback_allowed: bool,
) -> dict[str, object]:
    netbox_id = relation_id(vm.get("id"))
    if netbox_id is None:
        return vm
    candidates = sidecars_by_vm_id.get(netbox_id, [])
    if len(candidates) > 1:
        raise _selection_error(
            f"NetBox VM id {netbox_id} requires exactly one complete typed "
            f"Proxbox sync-state owner; found {len(candidates)}."
        )
    if candidates:
        return _overlay_selected_sidecar_identity(
            vm,
            candidates[0],
            netbox_id=netbox_id,
        )
    if legacy_fallback_allowed:
        warn_legacy_custom_fields("selected VM ownership fallback")
        return vm
    raise _selection_error(
        f"NetBox VM id {netbox_id} has no typed Proxbox sync-state owner "
        "and legacy custom-field fallback is disabled."
    )


async def _hydrate_selected_sidecar_identities(
    netbox_session: NetBoxSessionDep,
    vms: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Join explicit selections to authoritative sidecars in one scan."""

    selected_ids = {vm_id for vm in vms if (vm_id := relation_id(vm.get("id"))) is not None}
    if not selected_ids:
        return vms

    scan = await load_vm_sync_state_identities(netbox_session)
    legacy_fallback_allowed = custom_fields_enabled()
    if scan.sidecar_read_failed or scan.sidecar_unavailable:
        if legacy_fallback_allowed:
            warn_legacy_custom_fields("selected VM ownership fallback")
            return vms
        outcome = "failed" if scan.sidecar_read_failed else "is unavailable"
        raise _selection_error(
            "Typed Proxbox VM sync-state lookup "
            f"{outcome} while resolving selected NetBox VM id(s): "
            + ", ".join(str(vm_id) for vm_id in sorted(selected_ids))
            + "."
        )

    sidecars_by_vm_id: dict[int, list[dict[str, object]]] = {}
    for row in scan.rows:
        parent_id = relation_id(row.get("virtual_machine"))
        if parent_id in selected_ids:
            sidecars_by_vm_id.setdefault(parent_id, []).append(row)

    return [
        _hydrate_selected_vm_identity(
            vm,
            sidecars_by_vm_id=sidecars_by_vm_id,
            legacy_fallback_allowed=legacy_fallback_allowed,
        )
        for vm in vms
    ]


async def hydrate_selected_vm_identities(
    netbox_session: NetBoxSessionDep,
    vms: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Overlay authoritative sidecar identity onto explicitly selected VMs.

    Shared by selection paths outside this module (for example targeted backup
    sync) so every explicit selection resolves ownership sidecar-first with the
    same malformed/duplicate fail-closed and gated legacy-fallback semantics.
    """

    return await _hydrate_selected_sidecar_identities(netbox_session, vms)


def _resolve_selected_owner(
    vm: dict[str, object],
    *,
    netbox_id: int,
    endpoint_ids_by_cluster: dict[str, set[int | None]],
    require_stored_endpoint: bool = False,
) -> _SelectedVMOwner:
    vmid = normalize_positive_int(extract_proxmox_vmid(vm))
    vm_type = extract_proxmox_vm_type(vm)
    cluster_name = _normalize_cluster_name(relation_name(vm.get("cluster")))
    if vmid is None or vm_type is None or not cluster_name:
        raise _selection_error(
            f"NetBox VM id {netbox_id} has incomplete Proxmox ownership; "
            "cluster, positive VMID, and VM type are required, and endpoint identity "
            "must be stored or uniquely inferable."
        )

    source_endpoint_ids = endpoint_ids_by_cluster.get(cluster_name, set())
    if not source_endpoint_ids:
        raise _selection_error(
            f"No available Proxmox source owns cluster {cluster_name!r} "
            f"for NetBox VM id {netbox_id}."
        )
    if len(source_endpoint_ids) != 1 or None in source_endpoint_ids:
        displayed_endpoint_ids = sorted(
            "unknown" if endpoint_id is None else str(endpoint_id)
            for endpoint_id in source_endpoint_ids
        )
        raise _selection_error(
            f"Cluster {cluster_name!r} for NetBox VM id {netbox_id} is ambiguous "
            f"across endpoint ids {displayed_endpoint_ids}."
        )

    source_endpoint_id = next(iter(source_endpoint_ids))
    assert source_endpoint_id is not None
    endpoint_id = extract_proxmox_endpoint_id(vm)
    if endpoint_id is None:
        if require_stored_endpoint:
            raise _selection_error(
                f"NetBox VM id {netbox_id} has no stored Proxmox endpoint id; "
                "targeted sync requires endpoint, cluster, VMID, and VM type ownership."
            )
        # Compatibility for records created before endpoint identity was
        # stored. The cluster may supply the owner only when exactly one
        # available endpoint can possibly own it.
        endpoint_id = source_endpoint_id
    elif endpoint_id != source_endpoint_id:
        raise _selection_error(
            f"NetBox VM id {netbox_id} claims endpoint id {endpoint_id}, but "
            f"cluster {cluster_name!r} is owned by endpoint id {source_endpoint_id}."
        )

    return _SelectedVMOwner(
        netbox_id=netbox_id,
        endpoint_id=endpoint_id,
        cluster_name=cluster_name,
        vmid=vmid,
        vm_type=vm_type,
    )


def _resolve_selected_owners(
    vms: list[dict[str, object]],
    *,
    requested_ids: list[int],
    endpoint_ids_by_cluster: dict[str, set[int | None]],
    require_stored_endpoint: bool = False,
) -> list[_SelectedVMOwner]:
    records_by_id = {vm_id: vm for vm in vms if (vm_id := relation_id(vm.get("id"))) is not None}
    missing_ids = sorted(set(requested_ids).difference(records_by_id))
    if missing_ids:
        raise _selection_error(
            "NetBox did not return explicitly selected VM id(s): "
            + ", ".join(str(vm_id) for vm_id in missing_ids)
            + "."
        )

    owners: list[_SelectedVMOwner] = []
    owner_to_netbox_id: dict[tuple[int, str, int, str], int] = {}
    for netbox_id in requested_ids:
        owner = _resolve_selected_owner(
            records_by_id[netbox_id],
            netbox_id=netbox_id,
            endpoint_ids_by_cluster=endpoint_ids_by_cluster,
            require_stored_endpoint=require_stored_endpoint,
        )
        conflicting_id = owner_to_netbox_id.get(owner.resource_key)
        if conflicting_id is not None and conflicting_id != netbox_id:
            raise _selection_error(
                f"NetBox VM ids {conflicting_id} and {netbox_id} claim the same "
                "Proxmox endpoint/cluster/VMID/type owner."
            )
        owner_to_netbox_id[owner.resource_key] = netbox_id
        owners.append(owner)
    return owners


def _filter_cluster_resources_by_owners(  # noqa: C901
    cluster_resources: list[dict[str, object]],
    *,
    owners: list[_SelectedVMOwner],
    endpoint_ids_by_cluster: dict[str, set[int | None]],
) -> list[dict[str, object]]:
    """Return exactly one live resource for every resolved selected owner."""

    owners_by_resource_key = {owner.resource_key: owner for owner in owners}
    match_counts = {owner.netbox_id: 0 for owner in owners}

    filtered: list[dict[str, object]] = []
    for cluster in cluster_resources:
        if not isinstance(cluster, dict):
            continue
        for cluster_key, resources in cluster.items():
            if not isinstance(resources, list):
                continue
            normalized_cluster = _normalize_cluster_name(cluster_key)
            source_endpoint_ids = endpoint_ids_by_cluster.get(normalized_cluster, set())
            if len(source_endpoint_ids) != 1 or None in source_endpoint_ids:
                continue
            endpoint_id = next(iter(source_endpoint_ids))
            assert endpoint_id is not None
            selected: list[dict[str, object]] = []
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                vm_type = str(resource.get("type") or "").strip().lower()
                vmid = normalize_positive_int(resource.get("vmid"))
                if vm_type not in ("qemu", "lxc") or vmid is None:
                    continue
                owner = owners_by_resource_key.get((endpoint_id, normalized_cluster, vmid, vm_type))
                if owner is None:
                    continue
                match_counts[owner.netbox_id] += 1
                selected.append(resource)
            if selected:
                filtered.append({cluster_key: selected})

    ambiguous_ids = sorted(vm_id for vm_id, count in match_counts.items() if count > 1)
    if ambiguous_ids:
        raise _selection_error(
            "Multiple live Proxmox resources matched explicitly selected NetBox VM id(s): "
            + ", ".join(str(vm_id) for vm_id in ambiguous_ids)
            + "."
        )
    unresolved_ids = sorted(vm_id for vm_id, count in match_counts.items() if count == 0)
    if unresolved_ids:
        raise _selection_error(
            "No exact live Proxmox resource matched explicitly selected NetBox VM id(s): "
            + ", ".join(str(vm_id) for vm_id in unresolved_ids)
            + "."
        )

    return filtered


async def filter_cluster_resources_for_selected_vm(
    vm_record: object,
    cluster_resources: list[dict[str, object]],
    *,
    netbox_session: NetBoxSessionDep,
    netbox_vm_id: int,
    pxs: object,
    cluster_status: object,
) -> list[dict[str, object]]:
    """Filter a targeted sync by its complete, stored ownership identity.

    Unlike batch compatibility mode, a targeted route may not infer a missing
    endpoint from the cluster and never falls back to a VM name. The selected
    NetBox record must own exactly one live endpoint/cluster/VMID/type tuple.
    """

    requested_ids = _requested_ids([netbox_vm_id])
    vm = to_mapping(vm_record)
    hydrated_vms = await _hydrate_selected_sidecar_identities(
        netbox_session,
        [vm],
    )
    endpoint_ids_by_cluster = _source_endpoint_ids_by_cluster(pxs, cluster_status)
    owners = _resolve_selected_owners(
        hydrated_vms,
        requested_ids=requested_ids,
        endpoint_ids_by_cluster=endpoint_ids_by_cluster,
        require_stored_endpoint=True,
    )
    return _filter_cluster_resources_by_owners(
        cluster_resources,
        owners=owners,
        endpoint_ids_by_cluster=endpoint_ids_by_cluster,
    )


async def filter_cluster_resources_by_netbox_vm_ids(  # noqa: C901
    netbox_session: NetBoxSessionDep,
    cluster_resources: list[dict[str, object]],
    netbox_vm_ids: list[int],
    *,
    pxs: object,
    cluster_status: object,
) -> list[dict[str, object]]:
    """Filter resources by exact selected NetBox VM ownership.

    Args:
        netbox_session: NetBox session
        cluster_resources: List of cluster resources
        netbox_vm_ids: NetBox VM IDs to filter by
        pxs: Available Proxmox sessions carrying endpoint identity
        cluster_status: Cluster status rows aligned with ``pxs``

    Returns:
        Filtered cluster resources
    """
    if not netbox_vm_ids:
        return []

    requested_ids = _requested_ids(netbox_vm_ids)
    vms = await list_netbox_virtual_machines_by_ids(netbox_session, requested_ids)
    vms = await _hydrate_selected_sidecar_identities(
        netbox_session,
        vms,
    )
    endpoint_ids_by_cluster = _source_endpoint_ids_by_cluster(pxs, cluster_status)
    owners = _resolve_selected_owners(
        vms,
        requested_ids=requested_ids,
        endpoint_ids_by_cluster=endpoint_ids_by_cluster,
    )
    return _filter_cluster_resources_by_owners(
        cluster_resources,
        owners=owners,
        endpoint_ids_by_cluster=endpoint_ids_by_cluster,
    )


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
