"""Schema tests for intent apply payloads."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from proxbox_api.routes.intent.schemas import (
    ApplyDiff,
    ApplyRequest,
    ApplyResponse,
    VMIntentPayload,
)


def test_vm_intent_payload_accepts_minimal_required_fields():
    payload = VMIntentPayload(vmid=101, node="pve01", name="vm-101")

    assert payload.vmid == 101
    assert payload.node == "pve01"
    assert payload.name == "vm-101"
    assert payload.disks == []
    assert payload.nics == []


def test_apply_request_requires_run_uuid_and_diffs():
    diff = ApplyDiff(
        op="create",
        kind="qemu",
        payload=VMIntentPayload(vmid=101, node="pve01", name="vm-101"),
    )
    request = ApplyRequest(run_uuid="run-1", diffs=[diff])
    assert request.run_uuid == "run-1"
    assert request.diffs == [diff]

    with pytest.raises(ValidationError):
        ApplyRequest(diffs=[])

    with pytest.raises(ValidationError):
        ApplyRequest(run_uuid="run-1")


def test_apply_response_overall_is_constrained_to_known_literals():
    response = ApplyResponse(run_uuid="run-1", overall="no_op", results=[])
    assert response.overall == "no_op"

    with pytest.raises(ValidationError):
        ApplyResponse(run_uuid="run-1", overall="queued", results=[])
