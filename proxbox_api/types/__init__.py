"""Type definitions for proxbox-api."""

from proxbox_api.types.aliases import *
from proxbox_api.types.protocols import *

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
