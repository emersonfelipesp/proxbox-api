"""Proxmox node endpoints and node interface response schemas."""

from enum import Enum
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from proxmox_sdk.sdk.exceptions import ResourceException
from pydantic import BaseModel, field_validator

from proxbox_api.constants import NODE_PATTERN
from proxbox_api.enum.proxmox import AddressingMethod
from proxbox_api.exception import ProxboxException
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.schemas._coerce import normalize_bool
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session_for_request
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


class NodeSchema(BaseModel):
    node: str
    status: str
    cpu: float
    level: str | None = None
    maxcpu: int
    maxmem: float
    mem: float
    ssl_fingerprint: str


NodeSchemaList = list[dict[str, NodeSchema]]


@router.get("/", response_model=NodeSchemaList)
async def get_node(pxs: ProxmoxSessionsDep) -> NodeSchemaList:
    # Return all
    result: list[dict[str, NodeSchema]] = []
    for px in pxs:
        nodes = await resolve_async(px.session("/nodes/").get())
        if isinstance(nodes, list) and nodes:
            result.append({px.name: NodeSchema(**nodes[0])})
    return NodeSchemaList(result)


ProxmoxNodeDep = Annotated[NodeSchemaList, Depends(get_node)]


class InterfaceTypeChoices(str, Enum):
    bridge = "bridge"
    bond = "bond"
    eth = "eth"
    alias = "alias"
    vlan = "vlan"
    OVSBridge = "OVSBridge"
    OVSBond = "OVSBond"
    OVSPort = "OVSPort"
    OVSIntPort = "OVSIntPort"
    any_bridge = "any_bridge"
    any_local_bridge = "any_local_bridge"


class ProxmoxNodeInterfaceSchema(BaseModel):
    active: bool | None = None
    address: str | None = None
    netmask: str | None = None
    gateway: str | None = None
    autostart: bool | None = None
    bond_miimon: int | None = None
    bond_mode: str | None = None
    slaves: str | None = None
    bridge_fd: str | None = None
    bridge_ports: str | None = None
    bridge_stp: str | None = None
    bridge_vlan_aware: bool | None = None
    cidr: str | None = None
    comments: str | None = None
    exists: bool | None = None
    families: list[str] | None = None
    iface: str | None = None
    method: AddressingMethod | str | None = None
    method6: AddressingMethod | str | None = None
    priority: int | None = None
    type: str | None = None
    vlan_id: str | None = None
    vlan_raw_device: str | None = None

    @field_validator("active", "autostart", "bridge_vlan_aware", "exists", mode="before")
    @classmethod
    def _coerce_bool(cls, value: object) -> bool | None:
        return normalize_bool(value)


ProxmoxNodeInterfaceSchemaList = list[ProxmoxNodeInterfaceSchema]


@router.get(
    "/{node}/network",
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
    response_model=ProxmoxNodeInterfaceSchemaList,
)
async def get_node_network(
    pxs: ProxmoxSessionsDep,
    node: Annotated[
        str, Path(title="Proxmox Node", description="Proxmox Node Name (ex. 'pve01').", pattern=NODE_PATTERN)
    ],
    cluster_name: Annotated[
        str | None,
        Query(
            title="Cluster Name",
            description="Optional cluster name to disambiguate multi-session deployments.",
        ),
    ] = None,
    type: Annotated[
        InterfaceTypeChoices, Query(title="Network Type", description="Network Type (ex. 'eth0').")
    ] = None,
) -> ProxmoxNodeInterfaceSchemaList:
    px = resolve_proxmox_session_for_request(
        pxs,
        cluster_name,
        resource_name="node network",
    )

    interfaces = []
    try:
        if type:
            node_networks = await resolve_async(px.session(f"/nodes/{node}/network").get(type=type))
        else:
            node_networks = await resolve_async(px.session(f"/nodes/{node}/network").get())
    except ResourceException as error:
        raise ProxboxException(
            message="Error getting node network interfaces from Proxmox",
            python_exception=str(error),
        )

    for interface in node_networks:
        vlan_id = interface.get("vlan-id")
        if vlan_id:
            interface.pop("vlan-id")
            interface["vlan_id"] = vlan_id

        vlan_raw_device = interface.get("vlan-raw-device")
        if vlan_raw_device:
            interface.pop("vlan-raw-device")
            interface["vlan_raw_device"] = vlan_raw_device

        interfaces.append(ProxmoxNodeInterfaceSchema(**interface))

    return ProxmoxNodeInterfaceSchemaList(interfaces)


ProxmoxNodeInterfacesDep = Annotated[ProxmoxNodeInterfaceSchemaList, Depends(get_node_network)]


@router.get("/{node}/qemu/{vmid}/firewall")
async def get_qemu_firewall(
    pxs: ProxmoxSessionsDep,
    node: Annotated[
        str,
        Path(
            title="Proxmox Node",
            description="Proxmox Node name (ex. 'pve01').",
            pattern=NODE_PATTERN,
        ),
    ],
    vmid: Annotated[int, Path(title="VM ID", description="Proxmox QEMU VM ID.")],
    cluster_name: Annotated[
        str | None,
        Query(
            title="Cluster Name",
            description="Optional cluster name to disambiguate multi-session deployments.",
        ),
    ] = None,
):
    px = resolve_proxmox_session_for_request(
        pxs,
        cluster_name,
        resource_name="qemu firewall",
    )

    try:
        result = await resolve_async(px.session(f"/nodes/{node}/qemu/{vmid}/firewall").get())
    except ResourceException as error:
        raise ProxboxException(
            message="Error fetching qemu firewall from Proxmox",
            python_exception=str(error),
        )

    return result


@router.get("/{node}/qemu")
async def node_qemu(
    pxs: ProxmoxSessionsDep,
    node: Annotated[
        str, Path(title="Proxmox Node", description="Proxmox Node name (ex. 'pve01').", pattern=NODE_PATTERN)
    ],
    cluster_name: Annotated[
        str | None,
        Query(
            title="Cluster Name",
            description="Optional cluster name to disambiguate multi-session deployments.",
        ),
    ] = None,
):
    px = resolve_proxmox_session_for_request(
        pxs,
        cluster_name,
        resource_name="node qemu list",
    )

    try:
        json_result = await resolve_async(px.session(f"/nodes/{node}/qemu").get())
    except ResourceException as error:
        raise ProxboxException(
            message="Error fetching qemu list for node from Proxmox",
            python_exception=str(error),
        )

    return [{px.name: json_result}]
