"""Fixture-driven Python/Rust VM queue parity tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from proxbox_api.proxmox_to_netbox.models import ProxmoxVmConfigInput
from proxbox_api.services.sync.reconciliation.rust_bridge import (
    build_vm_operation_queue_rust,
    rust_available,
)
from proxbox_api.services.sync.reconciliation.types import PreparedVMState
from proxbox_api.services.sync.reconciliation.vm_queue import (
    _adapt_to_dataclasses,
    _normalize_ops,
    build_vm_operation_queue_python,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_names() -> list[str]:
    return sorted(path.stem for path in FIXTURES.glob("*.json"))


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _prepared_state_from_fixture(data: dict[str, Any]) -> PreparedVMState:
    return PreparedVMState(
        cluster_name=data["cluster_name"],
        resource=data["resource"],
        vm_config=data.get("vm_config") or {},
        vm_config_obj=ProxmoxVmConfigInput.model_validate(data.get("vm_config") or {}),
        desired_payload=data["desired_payload"],
        lookup=data.get("lookup") or {},
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        vm_type=data["vm_type"],
    )


def _python_ops(data: dict[str, Any]):
    prepared_vms = [_prepared_state_from_fixture(item) for item in data["prepared_vms"]]
    return prepared_vms, build_vm_operation_queue_python(
        prepared_vms,
        data["netbox_snapshot"],
        **data["flags"],
    )


@pytest.mark.parametrize("fixture", _fixture_names())
def test_fixture_python_contract(fixture: str) -> None:
    data = _load_fixture(fixture)
    _prepared_vms, ops = _python_ops(data)
    expected = data["expected"]

    assert [op.method for op in ops] == expected["methods"]
    assert [op.patch_payload for op in ops] == expected["patch_payloads"]
    if "existing_ids" in expected:
        assert [
            op.existing_record["id"] for op in ops if op.existing_record is not None
        ] == expected["existing_ids"]


@pytest.mark.parametrize("fixture", _fixture_names())
def test_rust_matches_python(fixture: str) -> None:
    if not rust_available():
        pytest.skip("proxbox-reconcile-rs is not installed")

    data = _load_fixture(fixture)
    prepared_vms, py_ops = _python_ops(data)

    raw_rust = build_vm_operation_queue_rust(
        prepared_vms=prepared_vms,
        netbox_snapshot=data["netbox_snapshot"],
        flags=data["flags"],
    )
    rust_ops = _adapt_to_dataclasses(raw_rust, prepared_vms)

    assert _normalize_ops(rust_ops) == _normalize_ops(py_ops)
