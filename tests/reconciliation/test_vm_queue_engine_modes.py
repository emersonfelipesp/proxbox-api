"""Engine-mode tests for the optional Rust reconciliation bridge."""

from __future__ import annotations

import asyncio
import json

import pytest

from proxbox_api import runtime_settings
from proxbox_api.app.cache_routes import (
    get_cache_metrics_json,
    get_cache_metrics_prometheus,
)
from proxbox_api.services.sync.reconciliation import rust_bridge, vm_queue
from proxbox_api.services.sync.reconciliation.metrics import (
    get_reconciliation_metrics,
    increment_reconciliation_mismatch_total,
    reset_reconciliation_metrics,
)
from proxbox_api.services.sync.reconciliation.vm_queue import (
    RustOperationAdaptationError,
    _adapt_to_dataclasses,
    build_vm_operation_queue,
)
from tests.reconciliation.test_vm_queue_python import _prepared_vm, _snapshot_vm


@pytest.fixture(autouse=True)
def _reset_engine_state(monkeypatch):
    monkeypatch.delenv("PROXBOX_RECONCILIATION_ENGINE", raising=False)
    monkeypatch.delenv("PROXBOX_RECONCILIATION_COMPARE_STRICT", raising=False)
    monkeypatch.setattr(runtime_settings, "_load_settings", lambda: None)
    monkeypatch.setattr(rust_bridge, "_rust_build", None)
    reset_reconciliation_metrics()


def _rust_output(method: str = "CREATE", vmid: int = 100, vm_type: str = "qemu") -> bytes:
    return json.dumps(
        [
            {
                "method": method,
                "cluster_name": "cluster-a",
                "vmid": vmid,
                "vm_type": vm_type,
                "desired_payload": {},
                "existing_record": {"id": 9000} if method != "CREATE" else None,
                "patch_payload": {"memory": 4096} if method == "UPDATE" else {},
            }
        ]
    ).encode()


def test_default_python_mode_does_not_call_available_rust(monkeypatch) -> None:
    calls = 0

    def fake_rust_build(input_bytes: bytes) -> bytes:
        nonlocal calls
        calls += 1
        return _rust_output("UPDATE")

    monkeypatch.setattr(rust_bridge, "_rust_build", fake_rust_build)
    prepared = [_prepared_vm()]

    queue = build_vm_operation_queue(prepared, [])

    assert [op.method for op in queue] == ["CREATE"]
    assert calls == 0


def test_compare_mode_returns_python_output_and_records_mismatch(monkeypatch) -> None:
    monkeypatch.setenv("PROXBOX_RECONCILIATION_ENGINE", "compare")
    monkeypatch.setattr(rust_bridge, "_rust_build", lambda input_bytes: _rust_output("CREATE"))
    prepared = [_prepared_vm()]
    snapshot = [_snapshot_vm()]

    queue = build_vm_operation_queue(prepared, snapshot)

    assert [op.method for op in queue] == ["GET"]
    assert get_reconciliation_metrics()["proxbox_reconcile_mismatch_total"] == 1


def test_plugin_settings_can_select_compare_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_settings,
        "_load_settings",
        lambda: {"reconciliation_engine": "compare"},
    )
    monkeypatch.setattr(rust_bridge, "_rust_build", lambda input_bytes: _rust_output("CREATE"))
    prepared = [_prepared_vm()]
    snapshot = [_snapshot_vm()]

    queue = build_vm_operation_queue(prepared, snapshot)

    assert [op.method for op in queue] == ["GET"]
    assert get_reconciliation_metrics()["proxbox_reconcile_mismatch_total"] == 1


def test_engine_env_override_wins_over_plugin_settings(monkeypatch) -> None:
    calls = 0

    def fake_rust_build(input_bytes: bytes) -> bytes:
        nonlocal calls
        calls += 1
        return _rust_output("UPDATE")

    monkeypatch.setattr(
        runtime_settings,
        "_load_settings",
        lambda: {"reconciliation_engine": "rust"},
    )
    monkeypatch.setenv("PROXBOX_RECONCILIATION_ENGINE", "python")
    monkeypatch.setattr(rust_bridge, "_rust_build", fake_rust_build)

    queue = build_vm_operation_queue([_prepared_vm()], [])

    assert [op.method for op in queue] == ["CREATE"]
    assert calls == 0


def test_compare_mode_strict_raises_on_mismatch(monkeypatch) -> None:
    monkeypatch.setenv("PROXBOX_RECONCILIATION_ENGINE", "compare")
    monkeypatch.setenv("PROXBOX_RECONCILIATION_COMPARE_STRICT", "true")
    monkeypatch.setattr(rust_bridge, "_rust_build", lambda input_bytes: _rust_output("CREATE"))

    with pytest.raises(AssertionError, match="Rust/Python reconciliation mismatch"):
        build_vm_operation_queue([_prepared_vm()], [_snapshot_vm()])

    assert get_reconciliation_metrics()["proxbox_reconcile_mismatch_total"] == 1


def test_rust_mode_returns_adapted_rust_output_without_running_python(monkeypatch) -> None:
    monkeypatch.setenv("PROXBOX_RECONCILIATION_ENGINE", "rust")
    monkeypatch.setattr(rust_bridge, "_rust_build", lambda input_bytes: _rust_output("UPDATE"))
    monkeypatch.setattr(
        vm_queue,
        "build_vm_operation_queue_python",
        lambda *args, **kwargs: pytest.fail("python engine should not run in rust mode"),
    )

    queue = build_vm_operation_queue([_prepared_vm()], [])

    assert [op.method for op in queue] == ["UPDATE"]
    assert queue[0].patch_payload == {"memory": 4096}


def test_rust_mode_raises_when_native_extension_is_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("PROXBOX_RECONCILIATION_ENGINE", "rust")

    with pytest.raises(RuntimeError, match="proxbox-reconcile-rs is not installed"):
        build_vm_operation_queue([_prepared_vm()], [])


def test_adapter_uses_vm_type_for_qemu_lxc_same_vmid() -> None:
    prepared = [
        _prepared_vm(vmid=100, vm_type="qemu", name="qemu-100"),
        _prepared_vm(vmid=100, vm_type="lxc", name="lxc-100"),
    ]
    raw_ops = [
        {
            "method": "GET",
            "cluster_name": "cluster-a",
            "vmid": 100,
            "vm_type": "qemu",
            "existing_record": {"id": 3001},
            "patch_payload": {},
        },
        {
            "method": "GET",
            "cluster_name": "cluster-a",
            "vmid": 100,
            "vm_type": "lxc",
            "existing_record": {"id": 3002},
            "patch_payload": {},
        },
    ]

    queue = _adapt_to_dataclasses(raw_ops, prepared)

    assert queue[0].prepared is prepared[0]
    assert queue[1].prepared is prepared[1]


def test_adapter_reports_unknown_prepared_identity() -> None:
    with pytest.raises(RustOperationAdaptationError, match="unknown prepared VM identity"):
        _adapt_to_dataclasses(
            [
                {
                    "method": "GET",
                    "cluster_name": "cluster-a",
                    "vmid": 999,
                    "vm_type": "qemu",
                    "existing_record": {"id": 3001},
                    "patch_payload": {},
                }
            ],
            [_prepared_vm()],
        )


def test_reconciliation_mismatch_metric_is_exposed_through_cache_metrics() -> None:
    increment_reconciliation_mismatch_total()

    json_metrics = asyncio.run(get_cache_metrics_json())
    prometheus_response = asyncio.run(get_cache_metrics_prometheus())

    assert json_metrics["proxbox_reconcile_mismatch_total"] == 1
    assert b"proxbox_reconcile_mismatch_total 1" in prometheus_response.body
