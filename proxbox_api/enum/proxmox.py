"""Enum definitions for Proxmox API path and mode choices."""

from enum import Enum, IntEnum


class ProxmoxModeOptions(str, Enum):
    single = "single"
    multi = "multi"


class ProxmoxAccessMethod(str, Enum):
    """Per-endpoint transport access method.

    Orthogonal to the read/write trust axis (``ProxmoxEndpoint.allow_writes``):
    this controls *how* an endpoint may be reached, not *whether* writes are
    allowed.

    - ``api`` — Read and Write over the Proxmox HTTP API only. Default for
      newly-created endpoints.
    - ``api_ssh`` — Read and Write over the Proxmox HTTP API **plus** SSH. SSH
      is an optional complement to API.

    There is intentionally no "SSH only" member: SSH can never be enabled
    without API, so an SSH-only endpoint is structurally unrepresentable.
    """

    api = "api"
    api_ssh = "api_ssh"

    @classmethod
    def ssh_enabled(cls, value: "str | ProxmoxAccessMethod | None") -> bool:
        """Return True when ``value`` permits the SSH transport (``api_ssh``)."""
        if value is None:
            return False
        if isinstance(value, cls):
            return value is cls.api_ssh
        return str(value) == cls.api_ssh.value


class ProxmoxUpperPaths(str, Enum):
    access = "access"
    cluster = "cluster"
    nodes = "nodes"
    storage = "storage"
    version = "version"


class ProxmoxAccessPaths(str, Enum):
    domains = "domains"
    groups = "groups"
    openid = "openid"
    roles = "roles"
    tfa = "tfa"
    users = "users"
    acl = "acl"
    password = "password"
    permissions = "permissions"
    ticket = "ticket"


class ProxmoxClusterPaths(str, Enum):
    acme = "acme"
    backup = "backup"
    backup_info = "backup-info"
    ceph = "ceph"
    config = "config"
    firewall = "firewall"
    ha = "ha"
    jobs = "jobs"
    mapping = "mapping"
    metrics = "metrics"
    replication = "replication"
    sdn = "sdn"
    log = "log"
    nextid = "nextid"
    options = "options"
    resources = "resources"
    status = "status"
    tasks = "tasks"


class ClusterResourcesType(str, Enum):
    vm = "vm"
    storage = "storage"
    node = "node"
    sdn = "sdn"


class ClusterResourcesTypeResponse(str, Enum):
    node = "node"
    storage = "storage"
    pool = "pool"
    qemu = "qemu"
    lxc = "lxc"
    openvz = "openvz"
    sdn = "sdn"
    network = "network"


class ProxmoxNodesPaths(str, Enum):
    node = "node"


class ResourceType(Enum):
    node = "node"
    storage = "storage"
    pool = "pool"
    qemu = "qemu"
    lxc = "lxc"
    openvz = "openvz"
    sdn = "sdn"
    network = "network"


class NodeStatus(Enum):
    unknown = "unknown"
    online = "online"
    offline = "offline"


# ---------------------------------------------------------------------------
# Proxmox VM / guest statuses (raw values as returned by Proxmox API)
# ---------------------------------------------------------------------------


class ProxmoxVMStatus(str, Enum):
    """Proxmox virtual machine / container runtime status."""

    running = "running"
    stopped = "stopped"
    paused = "paused"
    suspended = "suspended"
    prelaunch = "prelaunch"


# ---------------------------------------------------------------------------
# Backup-related enums
# ---------------------------------------------------------------------------


class BackupMode(str, Enum):
    """vzdump backup mode (controls guest state during backup)."""

    snapshot = "snapshot"
    suspend = "suspend"
    stop = "stop"


class CompressionAlgorithm(str, Enum):
    """Compression algorithm used for backup archives."""

    zstd = "zstd"
    lzo = "lzo"
    gzip = "gzip"
    none = "0"  # Proxmox API uses "0" for no compression


class NotificationMode(str, Enum):
    """When to send backup notification emails."""

    always = "always"
    failure = "failure"
    auto = "auto"
    never = "never"


class PBSChangeDetectionMode(str, Enum):
    """Proxmox Backup Server change detection algorithm."""

    legacy = "legacy"
    data = "data"
    metadata = "metadata"


# ---------------------------------------------------------------------------
# Disk / storage format enums
# ---------------------------------------------------------------------------


class DiskFormat(str, Enum):
    """Disk image format."""

    qcow2 = "qcow2"
    raw = "raw"
    vmdk = "vmdk"
    subvol = "subvol"


# ---------------------------------------------------------------------------
# Network addressing enums
# ---------------------------------------------------------------------------


class AddressingMethod(str, Enum):
    """Network interface IP addressing method."""

    manual = "manual"
    static = "static"
    dhcp = "dhcp"
    loopback = "loopback"


# ---------------------------------------------------------------------------
# cgroup mode (discrete selector, not a boolean)
# ---------------------------------------------------------------------------


class CgroupMode(IntEnum):
    """Linux cgroup version used by the Proxmox host."""

    V1 = 1
    V2 = 2
