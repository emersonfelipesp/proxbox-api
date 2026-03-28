"""Pydantic schemas for Proxmox sessions and resource payloads."""

from pydantic import BaseModel, RootModel

from proxbox_api.enum.proxmox import NodeStatus, ResourceType


class ProxmoxTokenSchema(BaseModel):
    name: str | None = None
    value: str | None = None


class ProxmoxSessionSchema(BaseModel):
    ip_address: str | None = None
    domain: str | None = None
    http_port: int | None = None
    user: str | None = None
    password: str | None = None
    token: ProxmoxTokenSchema | None = None
    ssl: bool = False


ProxmoxMultiClusterConfig = RootModel[list[ProxmoxSessionSchema]]


#
# /cluster
#
class Resources(BaseModel):
    cgroup_mode: int = None
    content: str = None
    cpu: float = None
    disk: int = None
    hastate: str = None
    id: str
    level: str = None
    maxcpu: float = None
    maxdisk: int = None
    maxmem: int = None
    mem: int = None
    name: str = None
    node: str = None
    plugintype: str = None
    pool: str = None
    status: str = None
    storage: str = None
    type: ResourceType
    uptime: int = None
    vmid: int = None


ResourcesList = RootModel[list[Resources]]
ClusterResourcesList = RootModel[list[dict[str, ResourcesList]]]


#
# /nodes
#


class Node(BaseModel):
    node: str
    status: NodeStatus
    cpu: float = None
    level: str = None
    maxcpu: int = None
    maxmem: int = None
    mem: int = None
    ssl_fingerprint: str = None
    uptime: int = None


NodeList = RootModel[list[Node]]
ResponseNodeList = RootModel[list[dict[str, ResourcesList]]]
