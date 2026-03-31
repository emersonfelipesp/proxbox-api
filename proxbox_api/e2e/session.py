"""NetBox demo session utilities for e2e testing.

Provides functions to create NetBox sessions from demo credentials and manage
the 'proxbox e2e testing' tag.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from netbox_sdk.config import Config
    from netbox_sdk.facade import Api

from netbox_sdk.client import NetBoxApiClient

E2E_TAG_NAME = "proxbox e2e testing"
E2E_TAG_SLUG = "proxbox-e2e-testing"
E2E_TAG_COLOR = "4caf50"
E2E_TAG_DESCRIPTION = "Objects created during proxbox-api e2e testing"


class E2ENetBoxApiClient(NetBoxApiClient):
    """Same as ``NetBoxApiClient`` but without HTTP response caching.

    The SDK caches GET list responses (e.g. ``/api/dcim/sites/``) for up to 60s.
    Reconcile helpers often GET (empty), POST (create), then GET again with the
    same query; a cache hit returns the stale empty list, so the second pass
    tries POST again and NetBox rejects the duplicate.
    """

    def _cache_policy(
        self,
        *,
        method: str,
        path: str,
        query: Any = None,
        payload: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        return None


async def create_netbox_demo_session(config: "Config") -> "Api":
    """Create an async NetBox API session from demo credentials.

    Args:
        config: NetBox SDK Config with demo token and credentials.

    Returns:
        Async NetBox API instance ready for requests.
    """
    from netbox_sdk.config import resolved_token
    from netbox_sdk.facade import Api

    if not resolved_token(config):
        raise ValueError("Config must have valid token for demo session")

    client = E2ENetBoxApiClient(config)
    return Api(client=client)


async def ensure_e2e_tag(nb: "Api") -> dict[str, Any]:
    """Ensure the 'proxbox e2e testing' tag exists in NetBox.

    Creates the tag if it doesn't exist, or returns existing tag if it does.

    Args:
        nb: NetBox API instance.

    Returns:
        Dict with tag details (id, name, slug, url, etc.).
    """
    from proxbox_api.netbox_rest import ensure_tag_async

    tag = await ensure_tag_async(
        nb,
        name=E2E_TAG_NAME,
        slug=E2E_TAG_SLUG,
        color=E2E_TAG_COLOR,
        description=E2E_TAG_DESCRIPTION,
    )
    return {
        "id": tag.id,
        "name": tag.name,
        "slug": tag.slug,
        "color": tag.color,
        "url": tag.url,
    }


async def get_e2e_tag(nb: "Api") -> dict[str, Any] | None:
    """Get the 'proxbox e2e testing' tag if it exists.

    Args:
        nb: NetBox API instance.

    Returns:
        Tag dict if exists, None otherwise.
    """
    from proxbox_api.netbox_rest import rest_first_async

    tag = await rest_first_async(nb, "/api/extras/tags/", query={"slug": E2E_TAG_SLUG})
    if tag:
        return {
            "id": tag.id,
            "name": tag.name,
            "slug": tag.slug,
            "color": tag.color,
            "url": tag.url,
        }
    return None


async def list_objects_with_e2e_tag(
    nb: "Api",
    object_type: str,
) -> list[dict[str, Any]]:
    """List all objects of a type that have the e2e testing tag.

    Args:
        nb: NetBox API instance.
        object_type: NetBox API path (e.g., "/api/dcim/devices/").

    Returns:
        List of objects with the e2e testing tag.
    """
    from proxbox_api.netbox_rest import rest_list_async

    tag = await get_e2e_tag(nb)
    if not tag:
        return []

    results = await rest_list_async(
        nb,
        object_type,
        query={"tag": tag["id"], "limit": 500},
    )
    return [record.serialize() for record in results]


async def cleanup_e2e_objects(
    nb: "Api",
    object_types: list[str] | None = None,
) -> dict[str, int]:
    """Delete all objects created during e2e testing.

    Note: This is optional since NetBox demo resets daily.
    Use this for cleanup between test runs if needed.

    Args:
        nb: NetBox API instance.
        object_types: List of object types to clean up.
            Defaults to all proxbox-created types.

    Returns:
        Dict mapping object type to count of deleted objects.
    """
    from proxbox_api.netbox_rest import rest_list_async

    if object_types is None:
        object_types = [
            "/api/plugins/proxbox/backups/",
            "/api/virtualization/virtual-machines/",
            "/api/dcim/devices/",
            "/api/dcim/sites/",
            "/api/dcim/device-types/",
            "/api/dcim/device-roles/",
            "/api/dcim/manufacturers/",
            "/api/virtualization/clusters/",
            "/api/virtualization/cluster-types/",
        ]

    deleted_counts: dict[str, int] = {}

    for obj_type in object_types:
        try:
            objects = await rest_list_async(nb, obj_type, query={"limit": 500})
            count = 0
            for obj in objects:
                if obj.url:
                    try:
                        await obj.delete()
                        count += 1
                    except Exception:  # noqa: BLE001
                        pass
            deleted_counts[obj_type] = count
        except Exception:  # noqa: BLE001
            deleted_counts[obj_type] = 0

    return deleted_counts


def build_e2e_tag_refs(tag: dict[str, Any]) -> list[dict[str, Any]]:
    """Build tag refs list for NetBox API payloads.

    Args:
        tag: Tag dict from ensure_e2e_tag or get_e2e_tag.

    Returns:
        List containing tag ref dict for API payloads.
    """
    return [
        {
            "name": tag["name"],
            "slug": tag["slug"],
            "color": tag["color"],
        }
    ]
