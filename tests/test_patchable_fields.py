"""Tests that SyncOverwriteFlags propagates into the per-call `patchable_fields` set.

For each sync service that constructs `patchable_fields` from `overwrite_flags`,
we patch the underlying `rest_bulk_reconcile_async`/`rest_reconcile_async`
helper, call the service with a flag flipped to False, and assert the
corresponding key is dropped from the allowlist passed to NetBox.

The historical `overwrite_flags=None` path keeps every key patchable, which
preserves the always-overwrite behavior expected by old callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from proxbox_api.schemas.sync import SyncOverwriteFlags


@dataclass
class _Capture:
    """Captures keyword arguments handed to a patched reconcile helper."""

    patchable_fields: frozenset[str] | set[str] | None = None
    called: bool = False


@dataclass
class _FakeBulkResult:
    records: list[dict[str, Any]]
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0


def _make_bulk_capture(monkeypatch: pytest.MonkeyPatch, target: str) -> _Capture:
    """Patch a `rest_bulk_reconcile_async` symbol and return the capture."""
    capture = _Capture()

    async def _fake_bulk(*_args: Any, **kwargs: Any) -> _FakeBulkResult:
        capture.called = True
        capture.patchable_fields = kwargs.get("patchable_fields")
        return _FakeBulkResult(records=[])

    monkeypatch.setattr(target, _fake_bulk)
    return capture


# ---------------------------------------------------------------------------
# bulk_reconcile_vm_interfaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vm_interfaces_default_includes_tags_and_custom_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from proxbox_api.services.sync import network as network_module

    capture = _make_bulk_capture(
        monkeypatch, "proxbox_api.services.sync.network.rest_bulk_reconcile_async"
    )

    await network_module.bulk_reconcile_vm_interfaces(
        nb=object(),
        interface_payloads=[{"name": "eth0", "virtual_machine": 1}],
        overwrite_flags=SyncOverwriteFlags(),
    )

    assert capture.called
    assert "tags" in capture.patchable_fields
    assert "custom_fields" in capture.patchable_fields


@pytest.mark.asyncio
async def test_vm_interfaces_drops_tags_when_flag_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from proxbox_api.services.sync import network as network_module

    capture = _make_bulk_capture(
        monkeypatch, "proxbox_api.services.sync.network.rest_bulk_reconcile_async"
    )

    await network_module.bulk_reconcile_vm_interfaces(
        nb=object(),
        interface_payloads=[{"name": "eth0", "virtual_machine": 1}],
        overwrite_flags=SyncOverwriteFlags(overwrite_vm_interface_tags=False),
    )

    assert "tags" not in capture.patchable_fields
    assert "custom_fields" in capture.patchable_fields


@pytest.mark.asyncio
async def test_vm_interfaces_drops_custom_fields_when_flag_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from proxbox_api.services.sync import network as network_module

    capture = _make_bulk_capture(
        monkeypatch, "proxbox_api.services.sync.network.rest_bulk_reconcile_async"
    )

    await network_module.bulk_reconcile_vm_interfaces(
        nb=object(),
        interface_payloads=[{"name": "eth0", "virtual_machine": 1}],
        overwrite_flags=SyncOverwriteFlags(overwrite_vm_interface_custom_fields=False),
    )

    assert "tags" in capture.patchable_fields
    assert "custom_fields" not in capture.patchable_fields


@pytest.mark.asyncio
async def test_vm_interfaces_legacy_none_keeps_all_patchable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from proxbox_api.services.sync import network as network_module

    capture = _make_bulk_capture(
        monkeypatch, "proxbox_api.services.sync.network.rest_bulk_reconcile_async"
    )

    await network_module.bulk_reconcile_vm_interfaces(
        nb=object(),
        interface_payloads=[{"name": "eth0", "virtual_machine": 1}],
        overwrite_flags=None,
    )

    assert "tags" in capture.patchable_fields
    assert "custom_fields" in capture.patchable_fields


# ---------------------------------------------------------------------------
# bulk_reconcile_vm_interface_ips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vm_interface_ips_default_includes_status_tags_custom_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from proxbox_api.services.sync import network as network_module

    capture = _make_bulk_capture(
        monkeypatch, "proxbox_api.services.sync.network.rest_bulk_reconcile_async"
    )

    await network_module.bulk_reconcile_vm_interface_ips(
        nb=object(),
        ip_payloads=[{"address": "10.0.0.1/24"}],
        overwrite_flags=SyncOverwriteFlags(),
    )

    assert capture.patchable_fields == frozenset({"status", "tags", "custom_fields", "dns_name"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag_name", "missing_key"),
    [
        ("overwrite_ip_status", "status"),
        ("overwrite_ip_tags", "tags"),
        ("overwrite_ip_custom_fields", "custom_fields"),
        ("overwrite_ip_address_dns_name", "dns_name"),
    ],
)
async def test_vm_interface_ips_drops_key_when_flag_disabled(
    monkeypatch: pytest.MonkeyPatch,
    flag_name: str,
    missing_key: str,
) -> None:
    from proxbox_api.services.sync import network as network_module

    capture = _make_bulk_capture(
        monkeypatch, "proxbox_api.services.sync.network.rest_bulk_reconcile_async"
    )

    await network_module.bulk_reconcile_vm_interface_ips(
        nb=object(),
        ip_payloads=[{"address": "10.0.0.1/24"}],
        overwrite_flags=SyncOverwriteFlags(**{flag_name: False}),
    )

    assert missing_key not in capture.patchable_fields
    expected_remaining = {"status", "tags", "custom_fields", "dns_name"} - {missing_key}
    assert expected_remaining.issubset(capture.patchable_fields)


@pytest.mark.asyncio
async def test_vm_interface_ips_legacy_none_keeps_all_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from proxbox_api.services.sync import network as network_module

    capture = _make_bulk_capture(
        monkeypatch, "proxbox_api.services.sync.network.rest_bulk_reconcile_async"
    )

    await network_module.bulk_reconcile_vm_interface_ips(
        nb=object(),
        ip_payloads=[{"address": "10.0.0.1/24"}],
        overwrite_flags=None,
    )

    assert capture.patchable_fields == frozenset({"status", "tags", "custom_fields", "dns_name"})


# ---------------------------------------------------------------------------
# _compute_device_patchable_fields — single source of truth for both the bulk
# DCIM path and the per-VM `_ensure_device` path. Issue #342 was caused by the
# two paths diverging; these tests pin them in lock-step.
# ---------------------------------------------------------------------------


def test_device_patchable_defaults_include_all_overwriteable_keys() -> None:
    from proxbox_api.services.sync.device_ensure import _compute_device_patchable_fields

    fields = _compute_device_patchable_fields(
        SyncOverwriteFlags(),
        overwrite_device_role=True,
        overwrite_device_type=True,
        overwrite_device_tags=True,
    )

    assert fields == {
        "cluster",
        "status",
        "description",
        "custom_fields",
        "role",
        "device_type",
        "tags",
    }


def test_device_patchable_legacy_none_flags_keeps_status_description_custom_fields() -> None:
    """``overwrite_flags=None`` mirrors the always-overwrite legacy contract."""
    from proxbox_api.services.sync.device_ensure import _compute_device_patchable_fields

    fields = _compute_device_patchable_fields(
        None,
        overwrite_device_role=True,
        overwrite_device_type=True,
        overwrite_device_tags=True,
    )

    assert {"cluster", "status", "description", "custom_fields"}.issubset(fields)
    assert {"role", "device_type", "tags"}.issubset(fields)


@pytest.mark.parametrize(
    ("kwarg", "missing_key"),
    [
        ("overwrite_device_role", "role"),
        ("overwrite_device_type", "device_type"),
        ("overwrite_device_tags", "tags"),
    ],
)
def test_device_patchable_drops_key_when_positional_flag_false(
    kwarg: str, missing_key: str
) -> None:
    from proxbox_api.services.sync.device_ensure import _compute_device_patchable_fields

    args = {
        "overwrite_device_role": True,
        "overwrite_device_type": True,
        "overwrite_device_tags": True,
        kwarg: False,
    }
    fields = _compute_device_patchable_fields(SyncOverwriteFlags(), **args)

    assert missing_key not in fields
    assert "cluster" in fields  # always patchable


@pytest.mark.parametrize(
    ("flag_name", "missing_key"),
    [
        ("overwrite_device_status", "status"),
        ("overwrite_device_description", "description"),
        ("overwrite_device_custom_fields", "custom_fields"),
    ],
)
def test_device_patchable_drops_key_when_schema_flag_false(
    flag_name: str, missing_key: str
) -> None:
    from proxbox_api.services.sync.device_ensure import _compute_device_patchable_fields

    fields = _compute_device_patchable_fields(
        SyncOverwriteFlags(**{flag_name: False}),
        overwrite_device_role=True,
        overwrite_device_type=True,
        overwrite_device_tags=True,
    )

    assert missing_key not in fields


def test_device_patchable_helper_is_used_by_bulk_path() -> None:
    """Lock the single-source-of-truth invariant: the bulk path must call the helper.

    Regression for issue #342: prior code duplicated the allowlist construction
    inline, and the per-VM `_ensure_device` path skipped it entirely. Refactor
    introduced this shared helper; this test pins that the bulk path imports it.
    """
    import inspect

    from proxbox_api.services.sync import device_ensure

    source = inspect.getsource(device_ensure.ensure_proxmox_devices_bulk)
    assert "_compute_device_patchable_fields" in source


def test_device_patchable_helper_is_used_by_ensure_device() -> None:
    """The per-VM path that bug #342 reported on must use the same helper."""
    import inspect

    from proxbox_api.services.sync import device_ensure

    source = inspect.getsource(device_ensure._ensure_device)
    assert "_compute_device_patchable_fields" in source


# ---------------------------------------------------------------------------
# _compute_vm_patchable_fields — single source of truth for VM reconciliation
# allowlists across route, service, and individual-sync paths.
# ---------------------------------------------------------------------------


def test_vm_patchable_defaults_include_all_overwriteable_keys() -> None:
    from proxbox_api.services.sync.vm_helpers import _compute_vm_patchable_fields

    fields = _compute_vm_patchable_fields(SyncOverwriteFlags())

    assert fields == {
        "name",
        "cluster",
        "device",
        "vcpus",
        "memory",
        "disk",
        "status",
        "virtual_machine_type",
        "role",
        "tags",
        "description",
        "custom_fields",
    }


def test_vm_patchable_legacy_none_flags_keeps_all_keys() -> None:
    from proxbox_api.services.sync.vm_helpers import _compute_vm_patchable_fields

    fields = _compute_vm_patchable_fields(None)

    assert {
        "name",
        "cluster",
        "device",
        "vcpus",
        "memory",
        "disk",
        "status",
        "virtual_machine_type",
        "role",
        "tags",
        "description",
        "custom_fields",
    }.issubset(fields)


@pytest.mark.parametrize(
    ("flag_name", "missing_key"),
    [
        ("overwrite_vm_type", "virtual_machine_type"),
        ("overwrite_vm_role", "role"),
        ("overwrite_vm_tags", "tags"),
        ("overwrite_vm_description", "description"),
        ("overwrite_vm_custom_fields", "custom_fields"),
    ],
)
def test_vm_patchable_drops_key_when_schema_flag_false(flag_name: str, missing_key: str) -> None:
    from proxbox_api.services.sync.vm_helpers import _compute_vm_patchable_fields

    fields = _compute_vm_patchable_fields(SyncOverwriteFlags(**{flag_name: False}))

    assert missing_key not in fields
    assert {"name", "cluster", "device", "vcpus", "memory", "disk", "status"}.issubset(fields)
