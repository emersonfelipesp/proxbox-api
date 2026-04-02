"""NetBox-specific utility functions to eliminate code duplication."""

from proxbox_api.types.protocols import NetBoxRecord, TagLike


def get_safe_id(obj: object, default: int | None = 0) -> int | None:
    """Safely extract ID from an object, with fallback.

    Replaces the pattern: int(getattr(obj, "id", 0) or 0)

    Args:
        obj: Object with an 'id' attribute
        default: Default value if id is None or falsy

    Returns:
        Integer ID or default value
    """
    if obj is None:
        return default

    obj_id = getattr(obj, "id", None)
    if obj_id is None or obj_id == 0:
        return default

    return int(obj_id)


def get_safe_attr(obj: object, attr: str, default: object = None) -> object:
    """Safely extract attribute from an object.

    Args:
        obj: Object to extract attribute from
        attr: Attribute name
        default: Default value if attribute is missing or None

    Returns:
        Attribute value or default
    """
    if obj is None:
        return default

    value = getattr(obj, attr, None)
    return value if value is not None else default


def build_tag_refs(tags: list[TagLike] | None) -> list[dict[str, str]]:
    """Build list of tag reference dictionaries from tag objects.

    Standardizes tag reference construction across the codebase.

    Args:
        tags: List of tag-like objects

    Returns:
        List of tag reference dictionaries with name, slug, and color
    """
    if not tags:
        return []

    tag_refs = []
    for tag in tags:
        name = get_safe_attr(tag, "name")
        slug = get_safe_attr(tag, "slug")
        color = get_safe_attr(tag, "color")

        # Only include tags with both name and slug
        if name and slug:
            tag_refs.append(
                {
                    "name": name,
                    "slug": slug,
                    "color": color or "9e9e9e",  # Default gray color
                }
            )

    return tag_refs


def _relation_id(value: object) -> dict[str, int] | None:
    """Extract relation ID from a value.

    Args:
        value: Value that might contain an ID (dict, object, or primitive)

    Returns:
        Dictionary with 'id' key or None
    """
    if value is None:
        return None

    # If it's a dict with 'id' key
    if isinstance(value, dict):
        if "id" in value and value["id"] is not None:
            return {"id": int(value["id"])}
        return None

    # If it's an object with 'id' attribute
    obj_id = get_safe_id(value, default=None)
    if obj_id is not None:
        return {"id": obj_id}

    # Try to convert directly to int
    try:
        return {"id": int(value)}
    except (ValueError, TypeError):
        return None


def _relation_name(value: object) -> dict[str, str] | None:
    """Extract relation name from a value.

    Args:
        value: Value that might contain a name (dict, object, or string)

    Returns:
        Dictionary with 'name' key or None
    """
    if value is None:
        return None

    # If it's a dict with 'name' key
    if isinstance(value, dict):
        if "name" in value and value["name"]:
            return {"name": str(value["name"])}
        return None

    # If it's an object with 'name' attribute
    name = get_safe_attr(value, "name")
    if name:
        return {"name": str(name)}

    # If it's already a string
    if isinstance(value, str) and value:
        return {"name": value}

    return None


def normalize_record_to_dict(
    record: NetBoxRecord | dict[str, object] | None,
    fields: list[str] | None = None,
) -> dict[str, object]:
    """Normalize a NetBox record to a dictionary.

    Args:
        record: NetBox record object or dict
        fields: Optional list of field names to extract (default: ["name", "slug"])

    Returns:
        Dictionary with extracted fields
    """
    if record is None:
        return {}

    if fields is None:
        fields = ["name", "slug"]

    result = {}

    for field in fields:
        if isinstance(record, dict):
            value = record.get(field)
        else:
            value = get_safe_attr(record, field)

        if value is not None:
            result[field] = value

    return result


def extract_ids(objects: list[object] | None) -> list[int]:
    """Extract IDs from a list of objects.

    Args:
        objects: List of objects with 'id' attribute

    Returns:
        List of integer IDs
    """
    if not objects:
        return []

    ids = []
    for obj in objects:
        obj_id = get_safe_id(obj, default=None)
        if obj_id is not None:
            ids.append(obj_id)

    return ids
