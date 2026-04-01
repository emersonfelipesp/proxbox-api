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
]
