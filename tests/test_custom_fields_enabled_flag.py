from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pytest

from proxbox_api.exception import ProxboxException
from proxbox_api.proxmox_to_netbox.models import ProxmoxVmConfigInput
from proxbox_api.routes.virtualization.virtual_machines import sync_vm
from proxbox_api.schemas.sync import behavior_flags_from_query_params
from proxbox_api.services import custom_fields, verb_dispatch
from proxbox_api.services.sync import sync_state_reader, sync_state_writer
from proxbox_api.services.sync.sync_state_reader import (
    VIRTUAL_MACHINES_PATH,
    VM_SYNC_STATE_PATH,
    resolve_virtual_machine_by_sync_state,
)


def _plugin_flag(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(
        custom_fields,
        "get_plugin_bool",
        lambda *, settings_key, default: value,
    )


def _prepared_vm() -> sync_vm._PreparedVMState:
    return sync_vm._PreparedVMState(
        cluster_name="cluster-a",
        resource={"name": "vm-101", "vmid": 101, "type": "qemu"},
        vm_config={},
        vm_config_obj=ProxmoxVmConfigInput.model_validate({}),
        desired_payload={
            "name": "vm-101",
            "status": "active",
            "cluster": 10,
            "device": 20,
            "role": 30,
            "vcpus": 2,
            "memory": 2048,
            "disk": 30,
            "tags": [99],
            "custom_fields": {"proxmox_endpoint_id": 500, "proxmox_vm_id": 101},
        },
        lookup={"cf_proxmox_vm_id": 101, "cf_proxmox_endpoint_id": 500},
        now=datetime.now(timezone.utc),
        vm_type="qemu",
    )


@pytest.mark.asyncio
async def test_default_off_dispatch_create_strips_custom_fields_and_cf_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plugin_flag(monkeypatch, False)
    create_payloads: list[dict[str, object]] = []
    resolver_fallbacks: list[dict[str, object] | None] = []

    async def _fake_resolver(*_args: Any, **kwargs: Any):
        resolver_fallbacks.append(kwargs.get("fallback_query"))
        return None

    async def _fake_create(
        _nb: object,
        _path: str,
        payload: dict[str, object],
        *,
        lookup: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del lookup
        create_payloads.append(dict(payload))
        return {"id": 42, **payload}

    monkeypatch.setattr(sync_vm, "resolve_virtual_machine_by_sync_state", _fake_resolver)
    monkeypatch.setattr(sync_vm, "rest_create_async", _fake_create)
    monkeypatch.setattr(sync_vm, "resolve_netbox_write_concurrency", lambda: 1)

    resolved, failed = await sync_vm._dispatch_vm_operation_queue(
        object(),
        [sync_vm._NetBoxVMOperation(method="CREATE", prepared=_prepared_vm())],
        overwrite_vm_custom_fields=True,
        custom_fields_enabled_flag=False,
    )

    assert failed == set()
    assert resolver_fallbacks == [None]
    assert create_payloads and "custom_fields" not in create_payloads[0]
    assert resolved[("cluster-a", 101, "qemu")]["id"] == 42


@pytest.mark.asyncio
async def test_default_off_reader_does_not_query_cf_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plugin_flag(monkeypatch, False)
    calls: list[tuple[str, dict[str, object]]] = []

    async def _fake_list(_nb: object, path: str, *, query: dict[str, object] | None = None):
        calls.append((path, dict(query or {})))
        if path == VM_SYNC_STATE_PATH:
            return []
        raise AssertionError(f"unexpected legacy CF query: {path} {query}")

    monkeypatch.setattr(sync_state_reader, "rest_list_async", _fake_list)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    resolved = await resolve_virtual_machine_by_sync_state(
        object(),
        proxmox_vm_id=101,
        endpoint_id=500,
        fallback_query={"cf_proxmox_vm_id": 101, "cf_proxmox_endpoint_id": 500},
    )

    assert resolved is None
    assert calls == [
        (VM_SYNC_STATE_PATH, {"proxmox_vm_id": 101, "proxmox_endpoint_raw_id": 500, "limit": 2})
    ]


@pytest.mark.asyncio
async def test_legacy_reader_fallback_warns_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _plugin_flag(monkeypatch, True)
    caplog.set_level(logging.WARNING)
    # The "proxbox" logger sets propagate=False, so caplog (a root handler)
    # cannot see its records unless propagation is temporarily restored.
    monkeypatch.setattr(logging.getLogger("proxbox"), "propagate", True)

    async def _fake_list(_nb: object, path: str, *, query: dict[str, object] | None = None):
        if path == VM_SYNC_STATE_PATH:
            return []
        assert path == VIRTUAL_MACHINES_PATH
        assert query == {
            "cf_proxmox_vm_id": 101,
            "cf_proxmox_endpoint_id": 500,
            "limit": 2,
        }
        return [{"id": 42, "name": "vm-101", "custom_fields": {"proxmox_vm_id": 101}}]

    monkeypatch.setattr(sync_state_reader, "rest_list_async", _fake_list)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    with pytest.warns(DeprecationWarning, match="sync-state models"):
        resolved = await resolve_virtual_machine_by_sync_state(
            object(),
            proxmox_vm_id=101,
            endpoint_id=500,
            fallback_query={"cf_proxmox_vm_id": 101, "cf_proxmox_endpoint_id": 500},
        )

    assert resolved is not None
    assert resolved.source == "custom_fields"
    assert any("sync-state models" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_custom_field_reconcile_skips_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plugin_flag(monkeypatch, False)

    async def _unexpected_reconcile(*_args: Any, **_kwargs: Any):
        raise AssertionError("custom-field reconcile must be skipped")

    monkeypatch.setattr(
        custom_fields,
        "reconcile_custom_field_with_status",
        _unexpected_reconcile,
    )
    custom_fields.invalidate_custom_fields_cache()

    assert await custom_fields.reconcile_custom_fields(object(), force=True) == []


@pytest.mark.asyncio
async def test_custom_field_reconcile_warns_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _plugin_flag(monkeypatch, True)
    caplog.set_level(logging.WARNING)
    # The "proxbox" logger sets propagate=False, so caplog (a root handler)
    # cannot see its records unless propagation is temporarily restored.
    monkeypatch.setattr(logging.getLogger("proxbox"), "propagate", True)
    custom_fields.invalidate_custom_fields_cache()
    monkeypatch.setattr(
        custom_fields,
        "CUSTOM_FIELD_INVENTORY",
        ({"name": "proxmox_vm_id", "object_types": ["virtualization.virtualmachine"]},),
    )

    class _Record:
        def serialize(self) -> dict[str, object]:
            return {"id": 1, "name": "proxmox_vm_id"}

    async def _fake_reconcile(*_args: Any, **_kwargs: Any):
        return type("Result", (), {"record": _Record()})()

    monkeypatch.setattr(custom_fields, "reconcile_custom_field_with_status", _fake_reconcile)

    with pytest.warns(DeprecationWarning, match="sync-state models"):
        result = await custom_fields.reconcile_custom_fields(object(), force=True)

    assert result == [{"id": 1, "name": "proxmox_vm_id"}]
    assert any("sync-state models" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_sidecar_write_still_uses_custom_field_source_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plugin_flag(monkeypatch, False)
    captured: list[dict[str, object]] = []

    async def _fake_upsert(*_args: Any, **kwargs: Any):
        captured.append(dict(kwargs["payload"]))
        return captured[-1]

    monkeypatch.setattr(sync_state_writer, "_upsert_parent_sidecar", _fake_upsert)

    result = await sync_state_writer.write_virtual_machine_sync_state(
        object(),
        virtual_machine_id=42,
        custom_fields={
            "proxmox_vm_id": 101,
            "proxmox_vm_type": "qemu",
            "proxmox_endpoint_id": 500,
        },
        overwrite_custom_fields=True,
    )

    assert result == captured[0]
    assert captured == [
        {
            "proxmox_vm_id": 101,
            "proxmox_vm_type": "qemu",
            "proxmox_endpoint_raw_id": 500,
        }
    ]


@pytest.mark.asyncio
async def test_verb_dispatch_fail_closed_when_sidecar_unverifiable_and_cf_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plugin_flag(monkeypatch, False)

    async def _fake_list(_nb: object, path: str, *, query: dict[str, object] | None = None):
        del query
        if path == VM_SYNC_STATE_PATH:
            raise ProxboxException(
                message="NetBox REST request failed",
                detail="HTTP 503 Service Unavailable",
                http_status_code=503,
            )
        raise AssertionError(f"unexpected legacy CF query: {path}")

    monkeypatch.setattr(sync_state_reader, "rest_list_async", _fake_list)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    with pytest.raises(ProxboxException) as exc_info:
        await verb_dispatch.resolve_netbox_vm_id(
            object(),
            101,
            endpoint_id=500,
            fail_closed=True,
        )

    assert exc_info.value.http_status_code == 409
    assert exc_info.value.detail["reason"] == "netbox_vm_identity_unverifiable_for_audit"


def test_behavior_flag_query_overrides_plugin_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    _plugin_flag(monkeypatch, True)

    assert behavior_flags_from_query_params({}).custom_fields_enabled is True
    assert (
        behavior_flags_from_query_params({"custom_fields_enabled": "false"}).custom_fields_enabled
        is False
    )
