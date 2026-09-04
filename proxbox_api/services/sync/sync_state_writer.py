"""Best-effort writers for netbox-proxbox typed sync-state sidecars."""

from __future__ import annotations

from collections.abc import Mapping

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    clear_rest_get_cache_for_path,
    rest_create_async,
    rest_first_async,
    rest_patch_async,
)

VM_SYNC_STATE_PATH = "/api/plugins/proxbox/sync-state/virtual-machines/"
DEVICE_SYNC_STATE_PATH = "/api/plugins/proxbox/sync-state/devices/"
CLUSTER_SYNC_STATE_PATH = "/api/plugins/proxbox/sync-state/clusters/"
VIRTUAL_DISK_SYNC_STATE_PATH = "/api/plugins/proxbox/sync-state/virtual-disks/"
VM_INTERFACE_SYNC_STATE_PATH = "/api/plugins/proxbox/sync-state/vm-interfaces/"

_UNAVAILABLE_SIDECAR_PATHS: set[str] = set()
_SIDECAR_ABSENT_STATUSES = frozenset({404, 501})


def _int_record_id(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mapping_record_id(value: Mapping[str, object]) -> int | None:
    for key in ("id", "pk"):
        if key in value:
            return _record_id(value.get(key))
    return None


def _attribute_record_id(value: object) -> int | None:
    for attr_name in ("id", "pk", "_data", "json"):
        try:
            attr_value = getattr(value, attr_name)
        except Exception:  # noqa: BLE001 - defensive against SDK proxy objects
            continue
        if attr_name in {"id", "pk"} and attr_value is not None:
            return _record_id(attr_value)
        if isinstance(attr_value, Mapping):
            return _record_id(attr_value)
    return None


def _getter_record_id(value: object) -> int | None:
    getter = getattr(value, "get", None)
    if not callable(getter):
        return None
    for key in ("id", "pk"):
        try:
            attr_value = getter(key)
        except Exception:  # noqa: BLE001 - defensive against SDK proxy objects
            continue
        if attr_value is not None:
            return _record_id(attr_value)
    return None


def _serialized_record_id(value: object) -> int | None:
    for method_name in ("serialize", "dict", "model_dump"):
        method = getattr(value, method_name, None)
        if not callable(method):
            continue
        try:
            serialized = method()
        except Exception:  # noqa: BLE001 - defensive against SDK proxy objects
            continue
        if isinstance(serialized, Mapping):
            return _record_id(serialized)
    return None


def _record_id(value: object) -> int | None:
    if isinstance(value, Mapping):
        return _mapping_record_id(value)
    for extractor in (
        _attribute_record_id,
        _getter_record_id,
        _serialized_record_id,
        _int_record_id,
    ):
        record_id = extractor(value)
        if record_id is not None:
            return record_id
    return None


def _text_or_blank(value: object) -> str:
    text = str(value or "").strip()
    return text


def _relation_ref(value: object) -> dict[str, int] | None:
    relation_id = _record_id(value)
    if relation_id is None:
        return None
    return {"id": relation_id}


def _without_none(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if value is not None}


def reset_sidecar_availability_cache() -> None:
    """Clear the current sync-run memo of unavailable optional sidecar routes."""
    _UNAVAILABLE_SIDECAR_PATHS.clear()
    try:
        from proxbox_api.services.sync.sync_state_reader import (
            reset_sidecar_reader_availability_cache,
        )
    except ImportError:
        return
    reset_sidecar_reader_availability_cache()


def _record_to_dict(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, Mapping):
        return dict(value)
    for method_name in ("serialize", "dict", "model_dump"):
        method = getattr(value, method_name, None)
        if not callable(method):
            continue
        try:
            serialized = method()
        except Exception:  # noqa: BLE001 - defensive against SDK proxy objects
            continue
        if isinstance(serialized, dict):
            return serialized
        if isinstance(serialized, Mapping):
            return dict(serialized)
    json_value = getattr(value, "json", None)
    if isinstance(json_value, dict):
        return json_value
    if isinstance(json_value, Mapping):
        return dict(json_value)
    return None


def _http_status_candidates(value: object) -> set[int]:
    if value is None:
        return set()
    if isinstance(value, bool):
        return set()
    if isinstance(value, int):
        return {value}
    return set()


def _is_sidecar_unavailable(error: Exception) -> bool:
    statuses: set[int] = set()
    for value in (
        getattr(error, "status", None),
        getattr(error, "status_code", None),
        getattr(error, "http_status_code", None),
    ):
        statuses.update(_http_status_candidates(value))
    return bool(statuses & _SIDECAR_ABSENT_STATUSES)


def _is_sidecar_duplicate_conflict(error: Exception) -> bool:
    detail = getattr(error, "detail", None)
    message = getattr(error, "message", None)
    status = (
        getattr(error, "status", None)
        or getattr(error, "status_code", None)
        or getattr(error, "http_status_code", None)
    )
    text = " ".join(str(part) for part in (status, detail, message, error) if part).lower()
    return any(
        marker in text
        for marker in (
            "409",
            "conflict",
            "sync_state_conflict",
            "already has a proxbox sync-state row",
            "target parent already has",
            "already exists",
            "must be unique",
            "make a unique set",
            "duplicate key value",
            "unique constraint",
        )
    )


async def _lookup_parent_sidecar(
    nb: object,
    *,
    path: str,
    parent_field: str,
    parent_id: int,
) -> object | None:
    return await rest_first_async(
        nb,
        path,
        query={f"{parent_field}_id": parent_id, "limit": 2},
    )


async def _patch_parent_sidecar(
    nb: object,
    *,
    path: str,
    parent_field: str,
    parent_id: int,
    existing: object,
    payload: dict[str, object],
) -> dict[str, object] | None:
    existing_id = _record_id(existing)
    if existing_id is None:
        logger.warning(
            "Skipping Proxbox sync-state patch for %s parent %s=%s: matched row has no id",
            path,
            parent_field,
            parent_id,
        )
        return None
    return _record_to_dict(await rest_patch_async(nb, path, existing_id, payload))


def _normalize_parent_id(*, path: str, parent_id: object, required: bool) -> int | None:
    normalized_parent_id = _record_id(parent_id)
    if normalized_parent_id is None and required:
        raise ValueError(f"Cannot persist required {path} state without a parent id")
    return normalized_parent_id


def _handle_sidecar_write_failure(*, path: str, error: Exception, required: bool) -> None:
    if _is_sidecar_unavailable(error):
        _UNAVAILABLE_SIDECAR_PATHS.add(path)
        logger.warning(
            "Skipping Proxbox sync-state sidecar write because %s is unavailable: %s",
            path,
            getattr(error, "detail", str(error)),
        )
    else:
        logger.warning(
            "Proxbox sync-state sidecar write failed at %s; %s: %s",
            path,
            (
                "required ownership state will be retried or surfaced as VM failure"
                if required
                else "optional reflection sync will continue"
            ),
            getattr(error, "detail", str(error)),
        )
    if required:
        raise error


async def _upsert_parent_sidecar(
    nb: object,
    *,
    path: str,
    parent_field: str,
    parent_id: object,
    payload: dict[str, object],
    required: bool = False,
) -> dict[str, object] | None:
    normalized_parent_id = _normalize_parent_id(
        path=path,
        parent_id=parent_id,
        required=required,
    )
    if normalized_parent_id is None:
        return None
    if path in _UNAVAILABLE_SIDECAR_PATHS:
        if required:
            raise RuntimeError(f"Required Proxbox sync-state sidecar path is unavailable: {path}")
        return None

    try:
        existing = await _lookup_parent_sidecar(
            nb,
            path=path,
            parent_field=parent_field,
            parent_id=normalized_parent_id,
        )
        if existing is not None:
            return await _patch_parent_sidecar(
                nb,
                path=path,
                parent_field=parent_field,
                parent_id=normalized_parent_id,
                existing=existing,
                payload=payload,
            )

        try:
            created = await rest_create_async(
                nb,
                path,
                {parent_field: {"id": normalized_parent_id}, **payload},
                lookup={f"{parent_field}_id": normalized_parent_id},
            )
            return _record_to_dict(created)
        except Exception as create_exc:
            if not _is_sidecar_duplicate_conflict(create_exc):
                raise
            try:
                clear_rest_get_cache_for_path(nb, path)
            except Exception:  # noqa: BLE001 - cache invalidation must not hide the upsert
                pass
            existing = await _lookup_parent_sidecar(
                nb,
                path=path,
                parent_field=parent_field,
                parent_id=normalized_parent_id,
            )
            if existing is None:
                raise
            return await _patch_parent_sidecar(
                nb,
                path=path,
                parent_field=parent_field,
                parent_id=normalized_parent_id,
                existing=existing,
                payload=payload,
            )
    except Exception as exc:  # noqa: BLE001 - optional sidecar writes remain best-effort
        _handle_sidecar_write_failure(path=path, error=exc, required=required)
        return None


def vm_sidecar_payload_from_custom_fields(
    custom_fields: Mapping[str, object],
) -> dict[str, object]:
    """Build the VM sidecar payload from the live VM custom-field payload."""
    payload: dict[str, object] = _without_none(
        {
            "proxmox_vm_id": custom_fields.get("proxmox_vm_id"),
            "proxmox_vm_type": custom_fields.get("proxmox_vm_type"),
            "proxmox_start_at_boot": custom_fields.get("proxmox_start_at_boot"),
            "proxmox_unprivileged_container": custom_fields.get("proxmox_unprivileged_container"),
            "proxmox_qemu_agent": custom_fields.get("proxmox_qemu_agent"),
            "proxmox_search_domain": custom_fields.get("proxmox_search_domain"),
            "proxmox_status": custom_fields.get("proxmox_status"),
            "proxmox_link": custom_fields.get("proxmox_link"),
            "proxmox_last_updated": custom_fields.get("proxmox_last_updated"),
            "proxmox_endpoint_raw_id": custom_fields.get("proxmox_endpoint_id"),
        }
    )
    if "proxmox_node" in custom_fields:
        payload["proxmox_node_name"] = _text_or_blank(custom_fields.get("proxmox_node"))
    if "proxmox_cluster" in custom_fields:
        payload["proxmox_cluster_name"] = _text_or_blank(custom_fields.get("proxmox_cluster"))
    raw_role_snapshot_id = custom_fields.get("proxmox_last_synced_role_id")
    role_snapshot_id = (
        None if isinstance(raw_role_snapshot_id, bool) else _record_id(raw_role_snapshot_id)
    )
    if role_snapshot_id is not None and role_snapshot_id > 0:
        payload["proxmox_last_synced_role_id"] = role_snapshot_id
    return payload


async def write_virtual_machine_sync_state(
    nb: object,
    *,
    virtual_machine_id: object,
    custom_fields: Mapping[str, object] | None,
    overwrite_custom_fields: bool,
    proxmox_vm_name: object = None,
    proxmox_last_synced_role_id: object = None,
) -> dict[str, object] | None:
    """Mirror VM state into the typed sidecar.

    ``proxmox_vm_name`` must be the name **Proxmox** reported for this guest --
    read it from the live Proxmox resource, never from the desired NetBox
    payload. The name-collision resolver may have rewritten that payload to
    preserve an operator's NetBox-side rename, so writing it back here would
    record the NetBox name as "what Proxmox last said" and permanently cement a
    stale value as ground truth (netbox-proxbox issue #617).

    ``proxmox_last_synced_role_id`` is likewise independent of
    ``overwrite_custom_fields``: it is role-ownership evidence, not an
    operator-managed reflection field. Callers provide it only after the
    corresponding VM reconciliation succeeds.
    """
    should_write_custom_field_state = overwrite_custom_fields and custom_fields is not None
    payload = (
        vm_sidecar_payload_from_custom_fields(custom_fields)
        if should_write_custom_field_state
        else {}
    )
    name = _text_or_blank(proxmox_vm_name)
    if name:
        payload["proxmox_vm_name"] = name
    role_id = (
        None
        if isinstance(proxmox_last_synced_role_id, bool)
        else _record_id(proxmox_last_synced_role_id)
    )
    if role_id is not None and role_id > 0:
        payload["proxmox_last_synced_role_id"] = role_id
    if not payload:
        return None
    role_snapshot_required = role_id is not None
    attempts = 3 if role_snapshot_required else 1
    for attempt in range(1, attempts + 1):
        try:
            result = await _upsert_parent_sidecar(
                nb,
                path=VM_SYNC_STATE_PATH,
                parent_field="virtual_machine",
                parent_id=virtual_machine_id,
                payload=payload,
                required=role_snapshot_required,
            )
            if result is not None or not role_snapshot_required:
                return result
            raise RuntimeError("Required VM role snapshot write returned no sidecar record")
        except Exception:
            if attempt >= attempts:
                raise
            logger.warning(
                "Retrying required VM role snapshot write for virtual_machine_id=%s "
                "after attempt %s/%s failed",
                virtual_machine_id,
                attempt,
                attempts,
            )
    raise AssertionError("unreachable VM sync-state retry state")


async def write_vm_role_snapshot_exact(
    nb: object,
    *,
    virtual_machine_id: object,
    snapshot_id: int | None,
    attempts: int = 3,
) -> None:
    """Restore and verify an exact role snapshot, including verified absence."""
    normalized_vm_id = _normalize_parent_id(
        path=VM_SYNC_STATE_PATH,
        parent_id=virtual_machine_id,
        required=True,
    )
    if normalized_vm_id is None:  # pragma: no cover - required normalization raises
        raise AssertionError("required VM id normalization returned None")
    normalized_snapshot_id = _record_id(snapshot_id) if snapshot_id is not None else None
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        # A failed required write may have memoized the route as unavailable.
        # Compensation must probe again instead of trusting process-local state.
        _UNAVAILABLE_SIDECAR_PATHS.discard(VM_SYNC_STATE_PATH)
        try:
            clear_rest_get_cache_for_path(nb, VM_SYNC_STATE_PATH)
        except Exception:  # noqa: BLE001 - cache clearing cannot replace verification
            pass
        try:
            await _upsert_parent_sidecar(
                nb,
                path=VM_SYNC_STATE_PATH,
                parent_field="virtual_machine",
                parent_id=normalized_vm_id,
                payload={"proxmox_last_synced_role_id": normalized_snapshot_id},
                required=True,
            )
        except Exception as exc:  # noqa: BLE001 - verify commit-before-response-loss below
            last_error = exc

        try:
            clear_rest_get_cache_for_path(nb, VM_SYNC_STATE_PATH)
        except Exception:  # noqa: BLE001 - cache clearing cannot replace verification
            pass
        from proxbox_api.services.sync.sync_state_reader import (
            read_vm_last_synced_role,
            reset_sidecar_reader_availability_cache,
        )

        reset_sidecar_reader_availability_cache()
        read = await read_vm_last_synced_role(
            nb,
            vm_record={"id": normalized_vm_id},
        )
        if read.verified and read.snapshot_id == normalized_snapshot_id:
            return
        last_error = RuntimeError("NetBox did not confirm the compensated VM role snapshot")
        logger.error(
            "VM role snapshot compensation attempt %s/%s failed for virtual_machine_id=%s: %s",
            attempt,
            max(1, attempts),
            normalized_vm_id,
            last_error,
        )
    raise RuntimeError(
        f"Could not restore VM {normalized_vm_id} role ownership snapshot"
    ) from last_error


async def write_vm_last_run_sync_state(
    nb: object,
    *,
    virtual_machine_id: object,
    run_id: str,
) -> dict[str, object] | None:
    return await _upsert_parent_sidecar(
        nb,
        path=VM_SYNC_STATE_PATH,
        parent_field="virtual_machine",
        parent_id=virtual_machine_id,
        payload={"last_run_id": run_id},
    )


async def write_device_sync_state(
    nb: object,
    *,
    device_id: object,
    proxmox_last_updated: object,
    proxmox_node_name: object = None,
    proxmox_cluster_name: object = None,
    overwrite_custom_fields: bool,
) -> dict[str, object] | None:
    if not overwrite_custom_fields:
        return None
    payload = _without_none({"proxmox_last_updated": proxmox_last_updated})
    if proxmox_node_name is not None:
        payload["proxmox_node_name"] = _text_or_blank(proxmox_node_name)
    if proxmox_cluster_name is not None:
        payload["proxmox_cluster_name"] = _text_or_blank(proxmox_cluster_name)
    return await _upsert_parent_sidecar(
        nb,
        path=DEVICE_SYNC_STATE_PATH,
        parent_field="device",
        parent_id=device_id,
        payload=payload,
    )


async def write_cluster_sync_state(
    nb: object,
    *,
    cluster_id: object,
    proxmox_last_updated: object,
    proxmox_cluster_name: object = None,
    overwrite_custom_fields: bool,
) -> dict[str, object] | None:
    if not overwrite_custom_fields:
        return None
    payload = _without_none({"proxmox_last_updated": proxmox_last_updated})
    if proxmox_cluster_name is not None:
        payload["proxmox_cluster_name"] = _text_or_blank(proxmox_cluster_name)
    return await _upsert_parent_sidecar(
        nb,
        path=CLUSTER_SYNC_STATE_PATH,
        parent_field="cluster",
        parent_id=cluster_id,
        payload=payload,
    )


async def write_virtual_disk_sync_state(
    nb: object,
    *,
    virtual_disk_id: object,
    proxbox_storage_id: object,
    overwrite_custom_fields: bool,
) -> dict[str, object] | None:
    if not overwrite_custom_fields:
        return None
    storage_id = _record_id(proxbox_storage_id)
    if storage_id is None:
        return None
    return await _upsert_parent_sidecar(
        nb,
        path=VIRTUAL_DISK_SYNC_STATE_PATH,
        parent_field="virtual_disk",
        parent_id=virtual_disk_id,
        payload={
            "proxbox_storage": _relation_ref(storage_id),
            "proxbox_storage_raw_id": None,
            "proxbox_storage_raw_value": "",
        },
    )


async def write_vm_interface_sync_state(
    nb: object,
    *,
    vm_interface_id: object,
    proxbox_bridge_id: object,
    overwrite_custom_fields: bool,
) -> dict[str, object] | None:
    if not overwrite_custom_fields:
        return None
    bridge_id = _record_id(proxbox_bridge_id)
    if bridge_id is None:
        return None
    return await _upsert_parent_sidecar(
        nb,
        path=VM_INTERFACE_SYNC_STATE_PATH,
        parent_field="vm_interface",
        parent_id=vm_interface_id,
        payload={
            "proxbox_bridge": _relation_ref(bridge_id),
            "proxbox_bridge_raw_id": None,
            "proxbox_bridge_raw_value": "",
        },
    )
