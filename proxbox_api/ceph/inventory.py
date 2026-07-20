"""Read-only RGW/RBD inventory helpers for the v1 ``/ceph/sync/*`` routes."""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any

_MISSING = object()


def _normalized_key(value: object) -> str:
    return str(value).lower().replace("_", "-")


_REDACT_KEYS = {
    _normalized_key(key)
    for key in (
        "access_key",
        "access_keys",
        "key",
        "keys",
        "secret_key",
        "secret_keys",
        "swift_key",
        "swift_keys",
    )
}


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_plain(item) for item in value]
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _plain(value.model_dump(mode="json"))
    if hasattr(value, "dict") and callable(value.dict):
        return _plain(value.dict())
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {
            str(key): _plain(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return value


def _redact(value: Any) -> Any:
    value = _plain(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if not isinstance(value, dict):
        return value

    redacted: dict[str, Any] = {}
    for key, item in value.items():
        normalized = _normalized_key(key)
        if (
            normalized in _REDACT_KEYS
            or "secret" in normalized
            or "password" in normalized
            or "token" in normalized
        ):
            redacted[str(key)] = "[redacted]"
        else:
            redacted[str(key)] = _redact(item)
    return redacted


def _as_list(value: Any) -> list[Any]:
    value = _plain(value)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple | set):
        return list(value)
    if isinstance(value, dict):
        if "data" in value and len(value) == 1:
            return _as_list(value["data"])
        return list(value.values())
    return [value]


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dedupe(items: Iterable[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[tuple[str, ...]] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        identity = tuple(str(item.get(key) or "") for key in keys)
        if identity in seen:
            continue
        seen.add(identity)
        out.append(item)
    return out


async def _maybe_call(target: Any, name: str, *args: Any) -> Any:
    method = getattr(target, name, None)
    if not callable(method):
        return _MISSING
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        try:
            signature.bind(*args)
        except TypeError:
            return _MISSING
    result = method(*args)
    if inspect.isawaitable(result):
        return await result
    return result


async def _first_call(target: Any, candidates: tuple[tuple[str, tuple[Any, ...]], ...]) -> Any:
    result = await _first_call_or_missing(target, candidates)
    if result is _MISSING:
        return []
    return result


async def _first_call_or_missing(
    target: Any, candidates: tuple[tuple[str, tuple[Any, ...]], ...]
) -> Any:
    for name, args in candidates:
        result = await _maybe_call(target, name, *args)
        if result is not _MISSING:
            return result
    return _MISSING


def _pool_has_application(pool: dict[str, Any], application: str) -> bool:
    values = (
        pool.get("application"),
        pool.get("application_list"),
        pool.get("application_metadata"),
        pool.get("applications"),
    )
    for value in values:
        if isinstance(value, str) and value.lower() == application:
            return True
        if isinstance(value, dict):
            lowered_keys = {str(key).lower() for key in value}
            lowered_values = {str(item).lower() for item in value.values()}
            if application in lowered_keys or application in lowered_values:
                return True
        if isinstance(value, list | tuple | set):
            for item in value:
                if isinstance(item, str) and item.lower() == application:
                    return True
                if isinstance(item, dict) and _pool_has_application(item, application):
                    return True
    return False


async def _application_pools(
    client: Any, nodes: list[str], application: str
) -> list[dict[str, Any]]:
    pools: list[dict[str, Any]] = []
    node_client = getattr(client, "nodes", None)
    if node_client is None:
        return pools
    for node in nodes:
        for raw_pool in _as_list(await node_client.pools(node)):
            if not isinstance(raw_pool, dict):
                continue
            pool = _redact(raw_pool)
            if _pool_has_application(pool, application):
                pools.append({**pool, "node": node})
    return _dedupe(pools, ("name", "pool_name"))


def _normalize_realm(raw: Any) -> dict[str, Any] | None:
    payload = _redact(raw)
    if not isinstance(payload, dict):
        return None
    name = _first(payload, "name", "realm", "id")
    if name is None:
        return None
    return {
        "name": str(name),
        "is_default": _bool(_first(payload, "is_default", "default")),
        "status": payload,
    }


def _normalize_zonegroup(raw: Any) -> dict[str, Any] | None:
    payload = _redact(raw)
    if not isinstance(payload, dict):
        return None
    name = _first(payload, "name", "zonegroup", "id")
    if name is None:
        return None
    return {
        "name": str(name),
        "realm_name": _first(payload, "realm_name", "realm"),
        "is_master": _bool(_first(payload, "is_master", "master")),
        "endpoints": _as_list(_first(payload, "endpoints", "endpoint")),
        "status": payload,
    }


def _normalize_zone(raw: Any) -> dict[str, Any] | None:
    payload = _redact(raw)
    if not isinstance(payload, dict):
        return None
    name = _first(payload, "name", "zone", "id")
    if name is None:
        return None
    return {
        "name": str(name),
        "zonegroup_name": _first(payload, "zonegroup_name", "zonegroup"),
        "endpoints": _as_list(_first(payload, "endpoints", "endpoint")),
        "status": payload,
    }


def _normalize_placement(raw: Any) -> dict[str, Any] | None:
    payload = _redact(raw)
    if not isinstance(payload, dict):
        return None
    name = _first(payload, "name", "key", "id")
    if name is None:
        return None
    return {
        "name": str(name),
        "zonegroup_name": _first(payload, "zonegroup_name", "zonegroup"),
        "zone_name": _first(payload, "zone_name", "zone"),
        "storage_classes": _as_list(_first(payload, "storage_classes", "storage_class")),
        "status": payload,
    }


def _normalize_user(raw: Any) -> dict[str, Any] | None:
    payload = _redact(raw)
    if not isinstance(payload, dict):
        return None
    uid = _first(payload, "uid", "user_id", "id")
    if uid is None:
        return None
    return {
        "uid": str(uid),
        "display_name": str(_first(payload, "display_name") or ""),
        "email": str(_first(payload, "email") or ""),
        "tenant": str(_first(payload, "tenant") or ""),
        "suspended": _bool(_first(payload, "suspended")),
        "max_buckets": _int_or_none(_first(payload, "max_buckets")),
        "status": payload,
    }


def _normalize_bucket(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        payload: dict[str, Any] = {"bucket": raw}
    else:
        payload = _redact(raw)
    if not isinstance(payload, dict):
        return None
    name = _first(payload, "name", "bucket")
    if name is None:
        return None
    size_bytes = _int_or_none(_first(payload, "size_bytes", "size"))
    if size_bytes is None:
        size_kb_actual = _int_or_none(_first(payload, "size_kb_actual"))
        size_bytes = size_kb_actual * 1024 if size_kb_actual is not None else None
    return {
        "name": str(name),
        "owner_uid": str(_first(payload, "owner_uid", "owner") or ""),
        "tenant": str(_first(payload, "tenant") or ""),
        "num_objects": _int_or_none(_first(payload, "num_objects")),
        "size_bytes": size_bytes,
        "placement_rule": str(_first(payload, "placement_rule") or ""),
        "versioning": str(_first(payload, "versioning") or ""),
        "status": payload,
    }


def _normalize_image(raw: Any, pool_name: str | None = None) -> dict[str, Any] | None:
    payload = _redact(raw)
    if not isinstance(payload, dict):
        return None
    name = _first(payload, "name", "image", "image_name")
    if name is None:
        return None
    features = _first(payload, "features", "features_name")
    return {
        "pool_name": str(_first(payload, "pool_name", "pool") or pool_name or ""),
        "name": str(name),
        "namespace": str(_first(payload, "namespace") or ""),
        "image_id": str(_first(payload, "image_id", "id") or ""),
        "size_bytes": _int_or_none(_first(payload, "size_bytes", "size")),
        "object_size": _int_or_none(_first(payload, "object_size", "obj_size")),
        "features": _as_list(features),
        "num_objects": _int_or_none(_first(payload, "num_objects", "num_objs")),
        "parent": _first(payload, "parent") or {},
        "data_pool": str(_first(payload, "data_pool") or ""),
        "status": payload,
    }


def _normalize_snapshot(
    raw: Any,
    *,
    image: dict[str, Any],
) -> dict[str, Any] | None:
    payload = _redact(raw)
    if not isinstance(payload, dict):
        return None
    name = _first(payload, "name", "snapshot", "snapshot_name", "snap_name")
    if name is None:
        return None
    return {
        "pool_name": image["pool_name"],
        "image_name": image["name"],
        "namespace": image["namespace"],
        "name": str(name),
        "snap_id": _int_or_none(_first(payload, "snap_id", "id")),
        "size_bytes": _int_or_none(_first(payload, "size_bytes", "size")),
        "protected": _bool(_first(payload, "protected", "is_protected")),
        "status": payload,
    }


def _string_or_none(value: Any) -> str | None:
    if value in (None, "") or isinstance(value, dict | list | tuple | set):
        return None
    return str(value)


def _split_rbd_ref(value: Any) -> tuple[str | None, str, str | None]:
    text = _string_or_none(value)
    if text is None:
        return None, "", None
    image_ref = text.rsplit("@", maxsplit=1)[0].strip()
    if not image_ref:
        return None, "", None
    parts = [part for part in image_ref.split("/") if part]
    if len(parts) >= 3:
        return parts[0], "/".join(parts[1:-1]), parts[-1]
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return None, "", parts[0]


def _split_rbd_snapshot_ref(value: Any) -> tuple[str | None, str, str | None, str | None]:
    text = _string_or_none(value)
    if text is None:
        return None, "", None, None
    image_ref, separator, snapshot_name = text.rpartition("@")
    if not separator:
        pool_name, namespace, image_name = _split_rbd_ref(text)
        return pool_name, namespace, image_name, None
    pool_name, namespace, image_name = _split_rbd_ref(image_ref)
    return pool_name, namespace, image_name, snapshot_name or None


def _rbd_image_ref(image: dict[str, Any]) -> str:
    pool_name = str(image.get("pool_name") or "")
    namespace = str(image.get("namespace") or "")
    name = str(image.get("name") or "")
    if namespace:
        return f"{pool_name}/{namespace}/{name}"
    return f"{pool_name}/{name}"


def _rbd_snapshot_ref(image: dict[str, Any], snapshot: dict[str, Any]) -> str:
    return f"{_rbd_image_ref(image)}@{snapshot['name']}"


def _normalize_clone(
    raw: Any,
    *,
    parent_image: dict[str, Any] | None = None,
    parent_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if isinstance(raw, str):
        payload: dict[str, Any] = {"child": raw}
    else:
        payload = _redact(raw)
    if not isinstance(payload, dict):
        return None

    parent_pool_name = _string_or_none(parent_image.get("pool_name")) if parent_image else None
    parent_namespace = _string_or_none(parent_image.get("namespace")) if parent_image else ""
    parent_image_name = _string_or_none(parent_image.get("name")) if parent_image else None
    parent_snapshot_name = _string_or_none(parent_snapshot.get("name")) if parent_snapshot else None

    parent_ref = _first(payload, "parent", "parent_ref", "source", "source_ref")
    if isinstance(parent_ref, dict):
        parent_pool_name = (
            _string_or_none(_first(parent_ref, "pool_name", "parent_pool_name", "pool"))
            or parent_pool_name
        )
        parent_namespace = _string_or_none(_first(parent_ref, "namespace", "parent_namespace")) or (
            parent_namespace or ""
        )
        parent_image_name = (
            _string_or_none(_first(parent_ref, "image_name", "parent_image_name", "image", "name"))
            or parent_image_name
        )
        parent_snapshot_name = (
            _string_or_none(
                _first(parent_ref, "snapshot_name", "parent_snapshot_name", "snapshot", "snap_name")
            )
            or parent_snapshot_name
        )
    else:
        pool_name, namespace, image_name, snapshot_name = _split_rbd_snapshot_ref(parent_ref)
        parent_pool_name = pool_name or parent_pool_name
        parent_namespace = namespace or (parent_namespace or "")
        parent_image_name = image_name or parent_image_name
        parent_snapshot_name = snapshot_name or parent_snapshot_name

    parent_image_field = payload.get("parent_image")
    if isinstance(parent_image_field, dict):
        parent_pool_name = (
            _string_or_none(_first(parent_image_field, "pool_name", "pool")) or parent_pool_name
        )
        parent_namespace = _string_or_none(_first(parent_image_field, "namespace")) or (
            parent_namespace or ""
        )
        parent_image_name = (
            _string_or_none(_first(parent_image_field, "image_name", "name", "image"))
            or parent_image_name
        )
    else:
        _pool, _namespace, _image_name = _split_rbd_ref(parent_image_field)
        parent_pool_name = _pool or parent_pool_name
        parent_namespace = _namespace or (parent_namespace or "")
        parent_image_name = _image_name or parent_image_name

    parent_snapshot_field = payload.get("parent_snapshot")
    if isinstance(parent_snapshot_field, dict):
        parent_snapshot_name = (
            _string_or_none(
                _first(parent_snapshot_field, "snapshot_name", "name", "snapshot", "snap_name")
            )
            or parent_snapshot_name
        )
    else:
        parent_snapshot_name = _string_or_none(parent_snapshot_field) or parent_snapshot_name

    parent_pool_name = (
        _string_or_none(_first(payload, "parent_pool_name", "parent_pool", "source_pool_name"))
        or parent_pool_name
    )
    parent_namespace = _string_or_none(
        _first(payload, "parent_namespace", "parent_image_namespace")
    ) or (parent_namespace or "")
    parent_image_name = (
        _string_or_none(_first(payload, "parent_image_name", "source_image_name"))
        or parent_image_name
    )
    parent_snapshot_name = (
        _string_or_none(
            _first(
                payload,
                "parent_snapshot_name",
                "source_snapshot_name",
                "snapshot_name",
                "snap_name",
            )
        )
        or parent_snapshot_name
    )

    child_ref = _first(
        payload,
        "child",
        "child_ref",
        "child_image",
        "child_image_ref",
        "clone",
        "clone_ref",
        "target",
        "target_ref",
    )
    if isinstance(child_ref, dict):
        child_pool_name = _string_or_none(_first(child_ref, "child_pool_name", "pool_name", "pool"))
        child_name = _string_or_none(_first(child_ref, "child_name", "image_name", "image", "name"))
    else:
        child_pool_name, _child_namespace, child_name = _split_rbd_ref(child_ref)

    child_pool_name = (
        _string_or_none(
            _first(payload, "child_pool_name", "child_pool", "target_pool_name", "clone_pool")
        )
        or child_pool_name
    )
    child_name = (
        _string_or_none(
            _first(
                payload,
                "child_name",
                "child_image_name",
                "target_image_name",
                "clone_name",
                "image_name",
                "image",
                "name",
            )
        )
        or child_name
    )
    child_pool_name = child_pool_name or _string_or_none(_first(payload, "pool_name", "pool"))
    child_pool_name = child_pool_name or parent_pool_name

    if not (
        parent_pool_name
        and parent_image_name
        and parent_snapshot_name
        and child_pool_name
        and child_name
    ):
        return None

    return {
        "parent_image": {
            "pool_name": parent_pool_name,
            "namespace": parent_namespace or "",
            "name": parent_image_name,
        },
        "parent_snapshot": {
            "pool_name": parent_pool_name,
            "namespace": parent_namespace or "",
            "image_name": parent_image_name,
            "name": parent_snapshot_name,
        },
        "parent_pool_name": parent_pool_name,
        "parent_namespace": parent_namespace or "",
        "parent_image_name": parent_image_name,
        "parent_snapshot_name": parent_snapshot_name,
        "child_pool_name": child_pool_name,
        "child_name": child_name,
        "status": payload,
    }


async def _collection(
    target: Any,
    candidates: tuple[tuple[str, tuple[Any, ...]], ...],
) -> list[Any]:
    return _as_list(await _first_call(target, candidates))


async def _rgw_buckets(rgw: Any) -> list[Any]:
    buckets = await _collection(rgw, (("buckets", ()), ("list_buckets", ())))
    get_bucket = getattr(rgw, "get_bucket", None)
    if not callable(get_bucket):
        return buckets

    detailed: list[Any] = []
    for bucket in buckets:
        if not isinstance(bucket, str):
            detailed.append(bucket)
            continue
        result = get_bucket(bucket)
        if inspect.isawaitable(result):
            result = await result
        detailed.append(result)
    return detailed


async def fetch_rgw_inventory(client: Any, nodes: list[str]) -> dict[str, Any]:
    """Collect RGW inventory from optional RGW helpers plus PVE RGW pools."""
    rgw = getattr(client, "rgw", client)
    dashboard = getattr(client, "dashboard", None)
    pools = await _application_pools(client, nodes, "rgw")
    bucket_source = rgw
    bucket_candidates: tuple[tuple[str, tuple[Any, ...]], ...] = (
        ("buckets", ()),
        ("list_buckets", ()),
    )
    if dashboard is not None and callable(getattr(dashboard, "rgw_buckets", None)):
        bucket_source = dashboard
        bucket_candidates = (("rgw_buckets", ()),)

    inventories = {
        "realms": [
            item
            for item in (
                _normalize_realm(raw)
                for raw in await _collection(rgw, (("realms", ()), ("list_realms", ())))
            )
            if item is not None
        ],
        "zonegroups": [
            item
            for item in (
                _normalize_zonegroup(raw)
                for raw in await _collection(
                    rgw,
                    (
                        ("zonegroups", ()),
                        ("zone_groups", ()),
                        ("list_zonegroups", ()),
                        ("list_zone_groups", ()),
                    ),
                )
            )
            if item is not None
        ],
        "zones": [
            item
            for item in (
                _normalize_zone(raw)
                for raw in await _collection(rgw, (("zones", ()), ("list_zones", ())))
            )
            if item is not None
        ],
        "placement_targets": [
            item
            for item in (
                _normalize_placement(raw)
                for raw in await _collection(
                    rgw,
                    (("placement_targets", ()), ("list_placement_targets", ())),
                )
            )
            if item is not None
        ],
        "users": [
            item
            for item in (
                _normalize_user(raw)
                for raw in await _collection(rgw, (("users", ()), ("list_users", ())))
            )
            if item is not None
        ],
        "buckets": [
            item
            for item in (
                _normalize_bucket(raw)
                for raw in (
                    await _rgw_buckets(rgw)
                    if bucket_source is rgw
                    else await _collection(bucket_source, bucket_candidates)
                )
            )
            if item is not None
        ],
        "pools": pools,
    }
    return {
        "realms": _dedupe(inventories["realms"], ("name",)),
        "zonegroups": _dedupe(inventories["zonegroups"], ("name",)),
        "zones": _dedupe(inventories["zones"], ("name",)),
        "placement_targets": _dedupe(inventories["placement_targets"], ("name",)),
        "users": _dedupe(inventories["users"], ("tenant", "uid")),
        "buckets": _dedupe(inventories["buckets"], ("tenant", "name")),
        "pools": pools,
    }


async def _rbd_images_for_pool(rbd: Any, pool_name: str | None) -> list[Any]:
    if pool_name:
        result = await _first_call_or_missing(
            rbd,
            (
                ("images", (pool_name,)),
                ("list_images", (pool_name,)),
                ("rbd_images", (pool_name,)),
            ),
        )
        if result is not _MISSING:
            return _as_list(result)
    return await _collection(rbd, (("images", ()), ("list_images", ()), ("rbd_images", ())))


async def _rbd_clones_for_pool(rbd: Any, pool_name: str | None) -> tuple[list[Any], bool]:
    if pool_name:
        result = await _first_call_or_missing(
            rbd,
            (
                ("clones", (pool_name,)),
                ("list_clones", (pool_name,)),
                ("rbd_clones", (pool_name,)),
            ),
        )
        if result is not _MISSING:
            return _as_list(result), False

    result = await _first_call_or_missing(
        rbd, (("clones", ()), ("list_clones", ()), ("rbd_clones", ()))
    )
    if result is not _MISSING:
        return _as_list(result), True
    return [], False


async def _rbd_children_for_snapshot(
    rbd: Any, image: dict[str, Any], snapshot: dict[str, Any]
) -> list[Any]:
    pool_name = str(image["pool_name"])
    image_name = str(image["name"])
    snapshot_name = str(snapshot["name"])
    image_ref = _rbd_image_ref(image)
    snapshot_ref = _rbd_snapshot_ref(image, snapshot)
    candidates: list[tuple[str, tuple[Any, ...]]] = []
    for method_name in ("children", "list_children", "rbd_children"):
        candidates.extend(
            [
                (method_name, (pool_name, image_name, snapshot_name)),
                (method_name, (image_ref, snapshot_name)),
                (method_name, (snapshot_ref,)),
            ]
        )
    return _as_list(await _first_call(rbd, tuple(candidates)))


async def _rbd_raw_images(
    rbd: Any,
    pool_names: list[str],
) -> list[tuple[Any, str | None]]:
    raw_images: list[tuple[Any, str | None]] = []
    if pool_names:
        for pool_name in pool_names:
            raw_images.extend(
                (item, pool_name) for item in await _rbd_images_for_pool(rbd, pool_name)
            )
    else:
        raw_images.extend((item, None) for item in await _rbd_images_for_pool(rbd, None))
    return raw_images


async def _rbd_clones_from_snapshot(
    rbd: Any,
    raw_snapshot: Any,
    *,
    image: dict[str, Any],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    clones: list[dict[str, Any]] = []
    raw_snapshot_payload = _plain(raw_snapshot)
    if isinstance(raw_snapshot_payload, dict):
        for raw_clone in _as_list(_first(raw_snapshot_payload, "clones", "children")):
            clone = _normalize_clone(
                raw_clone,
                parent_image=image,
                parent_snapshot=snapshot,
            )
            if clone is not None:
                clones.append(clone)
    for raw_child in await _rbd_children_for_snapshot(rbd, image, snapshot):
        clone = _normalize_clone(
            raw_child,
            parent_image=image,
            parent_snapshot=snapshot,
        )
        if clone is not None:
            clones.append(clone)
    return clones


async def _rbd_inventory_from_image(
    rbd: Any,
    raw_image: Any,
    pool_name: str | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    image = _normalize_image(raw_image, pool_name)
    if image is None:
        return None, [], []

    snapshots: list[dict[str, Any]] = []
    clones: list[dict[str, Any]] = []
    raw_payload = _plain(raw_image)
    if not isinstance(raw_payload, dict):
        return image, snapshots, clones

    for raw_snapshot in _as_list(raw_payload.get("snapshots")):
        snapshot = _normalize_snapshot(raw_snapshot, image=image)
        if snapshot is None:
            continue
        snapshots.append(snapshot)
        clones.extend(
            await _rbd_clones_from_snapshot(
                rbd,
                raw_snapshot,
                image=image,
                snapshot=snapshot,
            )
        )

    for raw_clone in _as_list(_first(raw_payload, "clones", "children")):
        clone = _normalize_clone(raw_clone, parent_image=image)
        if clone is not None:
            clones.append(clone)
    return image, snapshots, clones


async def _rbd_top_level_clones(
    rbd: Any,
    pool_names: list[str],
) -> list[dict[str, Any]]:
    clones: list[dict[str, Any]] = []
    global_clone_source_seen = False
    if pool_names:
        for pool_name in pool_names:
            raw_pool_clones, is_global_source = await _rbd_clones_for_pool(rbd, pool_name)
            if is_global_source:
                if global_clone_source_seen:
                    continue
                global_clone_source_seen = True
            for raw_clone in raw_pool_clones:
                clone = _normalize_clone(raw_clone)
                if clone is not None:
                    clones.append(clone)
        return clones

    raw_global_clones, _is_global_source = await _rbd_clones_for_pool(rbd, None)
    for raw_clone in raw_global_clones:
        clone = _normalize_clone(raw_clone)
        if clone is not None:
            clones.append(clone)
    return clones


async def fetch_rbd_inventory(client: Any, nodes: list[str]) -> dict[str, Any]:
    """Collect RBD image/snapshot inventory from optional RBD helpers and PVE pools."""
    rbd = getattr(client, "rbd", None) or getattr(client, "dashboard", None) or client
    pools = await _application_pools(client, nodes, "rbd")
    pool_names = [str(_first(pool, "name", "pool_name")) for pool in pools]

    images: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    clones: list[dict[str, Any]] = []
    for raw_image, pool_name in await _rbd_raw_images(rbd, pool_names):
        image, image_snapshots, image_clones = await _rbd_inventory_from_image(
            rbd,
            raw_image,
            pool_name,
        )
        if image is None:
            continue
        images.append(image)
        snapshots.extend(image_snapshots)
        clones.extend(image_clones)
    clones.extend(await _rbd_top_level_clones(rbd, pool_names))

    return {
        "pools": pools,
        "images": _dedupe(images, ("pool_name", "namespace", "name")),
        "snapshots": _dedupe(snapshots, ("pool_name", "namespace", "image_name", "name")),
        "clones": _dedupe(
            clones,
            (
                "parent_pool_name",
                "parent_namespace",
                "parent_image_name",
                "parent_snapshot_name",
                "child_pool_name",
                "child_name",
            ),
        ),
    }


def inventory_count(inventory: dict[str, Any], keys: tuple[str, ...]) -> int:
    return sum(len(_as_list(inventory.get(key))) for key in keys)


__all__ = [
    "fetch_rbd_inventory",
    "fetch_rgw_inventory",
    "inventory_count",
]
