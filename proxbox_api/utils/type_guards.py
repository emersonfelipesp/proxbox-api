"""Type guard functions for runtime type checking and validation."""

from typing import Any, TypeGuard

from proxbox_api.types.protocols import NetBoxRecord, ProxmoxResource, TagLike


def is_netbox_record(obj: Any) -> TypeGuard[NetBoxRecord]:
    """Check if an object conforms to the NetBoxRecord protocol.

    Args:
        obj: Object to check

    Returns:
        True if object has id, name, slug, and display attributes
    """
    return (
        hasattr(obj, "id")
        and hasattr(obj, "name")
        and hasattr(obj, "slug")
        and hasattr(obj, "display")
    )


def is_tag_like(obj: Any) -> TypeGuard[TagLike]:
    """Check if an object conforms to the TagLike protocol.

    Args:
        obj: Object to check

    Returns:
        True if object has name, slug, and color attributes
    """
    return hasattr(obj, "name") and hasattr(obj, "slug") and hasattr(obj, "color")


def is_proxmox_resource(obj: Any) -> TypeGuard[ProxmoxResource]:
    """Check if an object conforms to the ProxmoxResource protocol.

    Args:
        obj: Object to check

    Returns:
        True if object is dict-like (has get and __getitem__)
    """
    return hasattr(obj, "get") and hasattr(obj, "__getitem__")


def is_valid_id(value: Any) -> TypeGuard[int]:
    """Check if a value is a valid positive integer ID.

    Args:
        value: Value to check

    Returns:
        True if value is a positive integer
    """
    if not isinstance(value, int):
        try:
            value = int(value)
        except (ValueError, TypeError):
            return False

    return value > 0


def is_valid_ip(value: str) -> bool:
    """Check if a string is a valid IP address (v4 or v6).

    Args:
        value: String to check

    Returns:
        True if value is a valid IP address
    """
    import ipaddress

    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def is_valid_mac(value: str) -> bool:
    """Check if a string is a valid MAC address.

    Args:
        value: String to check

    Returns:
        True if value is a valid MAC address
    """
    import re

    # MAC address pattern: XX:XX:XX:XX:XX:XX or XX-XX-XX-XX-XX-XX
    pattern = r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$"
    return bool(re.match(pattern, value))


def is_valid_slug(value: str) -> bool:
    """Check if a string is a valid slug (URL-safe identifier).

    Args:
        value: String to check

    Returns:
        True if value is a valid slug
    """
    import re

    # Slug pattern: lowercase letters, numbers, hyphens
    pattern = r"^[a-z0-9-]+$"
    return bool(re.match(pattern, value))


def has_required_fields(obj: dict[str, Any], *fields: str) -> bool:
    """Check if a dictionary has all required fields with non-None values.

    Args:
        obj: Dictionary to check
        fields: Required field names

    Returns:
        True if all fields exist and are not None
    """
    return all(obj.get(field) is not None for field in fields)


def safe_dict_get(obj: Any, key: str, default: Any = None) -> Any:
    """Safely get a value from a dict-like object.

    Args:
        obj: Dict-like object
        key: Key to retrieve
        default: Default value if key is missing

    Returns:
        Value at key or default
    """
    if obj is None:
        return default

    if isinstance(obj, dict):
        return obj.get(key, default)

    if hasattr(obj, "get"):
        return obj.get(key, default)

    return default
