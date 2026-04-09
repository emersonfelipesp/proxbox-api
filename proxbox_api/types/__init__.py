"""Type definitions for proxbox-api."""

from proxbox_api.types.aliases import (
    VMID,
    ClusterName,
    IPAddress,
    MACAddress,
    NodeName,
    ProxmoxNodeName,
    RecordID,
    SyncStatus,
    VLANId,
    VMStatus,
)
from proxbox_api.types.protocols import (
    NetBoxRecord,
    ProxmoxResource,
    SyncResult,
    TagLike,
)
from proxbox_api.types.structured_dicts import (
    CacheEntryDict,
    DevicePayloadDict,
    InterfacePayloadDict,
    IPAddressPayloadDict,
    ProxboxSettingsDict,
    ProxmoxDeviceDict,
    ProxmoxVMDict,
    StoragePayloadDict,
    SyncPhaseResultDict,
    SyncResultDict,
    VMPayloadDict,
)

__all__ = [
    # Protocols
    "NetBoxRecord",
    "TagLike",
    "ProxmoxResource",
    "SyncResult",
    # Aliases
    "RecordID",
    "ClusterName",
    "NodeName",
    "VMID",
    "ProxmoxNodeName",
    "IPAddress",
    "MACAddress",
    "VLANId",
    "SyncStatus",
    "VMStatus",
    # TypedDicts
    "ProxboxSettingsDict",
    "SyncResultDict",
    "DevicePayloadDict",
    "VMPayloadDict",
    "InterfacePayloadDict",
    "IPAddressPayloadDict",
    "CacheEntryDict",
    "StoragePayloadDict",
    "ProxmoxDeviceDict",
    "ProxmoxVMDict",
    "SyncPhaseResultDict",
]
