"""Internal read-only Ceph facade over the existing Proxmox SDK client.

The public ``proxmox_sdk.ceph`` namespace is not available in the current
``proxmox-sdk==0.0.4.post2`` dependency.  Keep the proxbox-api ``/ceph/*``
routes operational by calling the same Proxmox VE API paths directly through
the already-authenticated SDK session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from proxmox_sdk.sdk.api import ProxmoxSDK
    from proxmox_sdk.sdk.resource import ProxmoxResource


class AttrDict(dict[str, Any]):
    """Dictionary with attribute access for SDK-like response objects."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _attrdict(value: Any) -> Any:
    if isinstance(value, dict):
        return AttrDict({key: _attrdict(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_attrdict(item) for item in value]
    return value


def _unwrap(data: Any) -> Any:
    if isinstance(data, dict) and len(data) == 1 and "data" in data:
        return data["data"]
    return data


def _normalize_list(data: Any, *, dict_mode: Literal["values", "items"] = "values") -> list[Any]:
    data = _unwrap(data)
    if data is None:
        return []
    if isinstance(data, list):
        return [_attrdict(item) for item in data]
    if isinstance(data, dict):
        if dict_mode == "items":
            return [
                AttrDict({"name": key, "value": _attrdict(value)}) for key, value in data.items()
            ]
        return [_attrdict(item) for item in data.values()]
    return [_attrdict(data)]


class ClusterCeph:
    """Cluster-wide Ceph read helpers."""

    def __init__(self, sdk: ProxmoxSDK) -> None:
        self._sdk = sdk

    async def status(self) -> AttrDict:
        return _attrdict(_unwrap(await self._sdk("cluster/ceph/status").get()) or {})

    async def metadata(self) -> Any:
        return _attrdict(_unwrap(await self._sdk("cluster/ceph/metadata").get()) or {})

    async def flags(self) -> list[Any]:
        return _normalize_list(await self._sdk("cluster/ceph/flags").get(), dict_mode="items")


class NodeCeph:
    """Node-scoped Ceph read helpers."""

    def __init__(self, sdk: ProxmoxSDK) -> None:
        self._sdk = sdk

    def _resource(self, node: str, *segments: str | int) -> ProxmoxResource:
        return self._sdk(["nodes", node, "ceph", *segments])

    async def _list_daemons(self, node: str, segment: str, daemon_type: str) -> list[Any]:
        return [
            AttrDict({**item, "type": daemon_type})
            for item in _normalize_list(await self._resource(node, segment).get())
            if isinstance(item, dict)
        ]

    async def monitors(self, node: str) -> list[Any]:
        return await self._list_daemons(node, "mon", "mon")

    async def managers(self, node: str) -> list[Any]:
        return await self._list_daemons(node, "mgr", "mgr")

    async def metadata_servers(self, node: str) -> list[Any]:
        return await self._list_daemons(node, "mds", "mds")

    async def osds(self, node: str) -> list[Any]:
        return _normalize_list(await self._resource(node, "osd").get())

    async def pools(self, node: str) -> list[Any]:
        return _normalize_list(await self._resource(node, "pool").get())

    async def filesystems(self, node: str) -> list[Any]:
        return _normalize_list(await self._resource(node, "fs").get())

    async def crush(self, node: str) -> Any:
        return _attrdict(_unwrap(await self._resource(node, "crush").get()))

    async def rules(self, node: str) -> list[Any]:
        return _normalize_list(await self._resource(node, "rules").get())


class CephClient:
    """Read-only Ceph client backed by an existing PVE Proxmox SDK session."""

    def __init__(self, sdk: ProxmoxSDK) -> None:
        self._sdk = sdk
        self.cluster = ClusterCeph(sdk)
        self.nodes = NodeCeph(sdk)

    @classmethod
    def from_sdk(cls, sdk: ProxmoxSDK) -> CephClient:
        return cls(sdk)

    async def status(self) -> AttrDict:
        return await self.cluster.status()


__all__ = ["AttrDict", "CephClient", "ClusterCeph", "NodeCeph"]
