"""Type aliases for common domain types."""

from typing import TypeAlias

# NetBox identifiers
RecordID: TypeAlias = int
ClusterName: TypeAlias = str
NodeName: TypeAlias = str

# Proxmox identifiers
VMID: TypeAlias = int
ProxmoxNodeName: TypeAlias = str

# Network types
IPAddress: TypeAlias = str
MACAddress: TypeAlias = str
VLANId: TypeAlias = int

# Status types
SyncStatus: TypeAlias = str  # "pending" | "in_progress" | "completed" | "failed"
VMStatus: TypeAlias = str  # "active" | "offline" | "staged"
