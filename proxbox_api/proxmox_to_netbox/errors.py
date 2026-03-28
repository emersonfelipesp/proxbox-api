"""Domain exceptions for Proxmox-to-NetBox transformation workflows."""


class ProxmoxToNetBoxError(Exception):
    """Raised when Proxmox raw payload cannot be transformed safely."""
