"""Application-wide constants and configuration values."""

# NetBox API pagination
NETBOX_PAGE_SIZE = 200
NETBOX_MAX_OFFSET = 10_000

# NetBox typed-schema version targeted by netbox-sdk's `build_schema_index`.
# Bump alongside the netbox-sdk pin and the OpenAPI snapshots when targeting a new NetBox release.
NETBOX_SCHEMA_VERSION = "4.6"

# VM sync defaults
DEFAULT_VM_STATUS = "active"
DEFAULT_VM_ROLE = "undefined"

# VM type mappings for NetBox VirtualMachineType objects (NetBox v4.6+)
VM_TYPE_MAPPINGS = {
    "qemu": {
        "name": "QEMU Virtual Machine",
        "slug": "qemu-virtual-machine",
        "description": "Proxmox QEMU/KVM Virtual Machine",
    },
    "lxc": {
        "name": "LXC Container",
        "slug": "lxc-container",
        "description": "Proxmox LXC Container",
    },
}

# VM role mappings for NetBox device-role objects representing Proxmox VM types.
# Single source of truth — services and route helpers import this; callers
# extend with `tags` at the call site since tag refs vary per sync run.
VM_ROLE_MAPPINGS: dict[str, dict[str, object]] = {
    "qemu": {
        "name": "Virtual Machine (QEMU)",
        "slug": "virtual-machine-qemu",
        "color": "00ffff",
        "description": "Proxmox Virtual Machine",
        "vm_role": True,
    },
    "lxc": {
        "name": "Container (LXC)",
        "slug": "container-lxc",
        "color": "7fffd4",
        "description": "Proxmox LXC Container",
        "vm_role": True,
    },
    "undefined": {
        "name": "Unknown",
        "slug": "unknown",
        "color": "000000",
        "description": "VM Type not found. Neither QEMU nor LXC.",
        "vm_role": True,
    },
}

# Network configuration
DEFAULT_TAG_COLOR = "9e9e9e"  # Gray
MAX_NETWORK_INTERFACES = 100  # Proxmox supports up to 100 network interfaces per VM

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2
RETRY_EXPONENTIAL_BASE = 2

# Timeouts (in seconds)
DEFAULT_API_TIMEOUT = 30
LONG_OPERATION_TIMEOUT = 300
WEBSOCKET_TIMEOUT = 60

# Sync operation defaults
DEFAULT_BATCH_SIZE = 50
CONCURRENT_SYNC_LIMIT = 10

# Tag names and slugs
PROXMOX_TAG = "proxmox"
PROXBOX_TAG = "proxbox"
AUTO_SYNCED_TAG = "auto-synced"

# Status codes
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_NO_CONTENT = 204
HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_INTERNAL_ERROR = 500

# File paths
DEFAULT_DB_PATH = "database.db"
DEFAULT_LOG_PATH = "/var/log/proxbox.log"

# Proxmox node name validation — must start with alphanumeric, then allow dots/hyphens/underscores.
# Applied to all `node` path parameters to prevent path traversal and injection.
NODE_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$"

# Proxmox API versions
SUPPORTED_PROXMOX_VERSIONS = ["8.1", "8.2", "8.3", "latest"]
DEFAULT_PROXMOX_VERSION = "latest"

# NetBox object types
NETBOX_DEVICE_TYPE = "device"
NETBOX_VM_TYPE = "virtual_machine"
NETBOX_INTERFACE_TYPE = "interface"
NETBOX_IP_TYPE = "ip_address"
NETBOX_CLUSTER_TYPE = "cluster"
NETBOX_SITE_TYPE = "site"
