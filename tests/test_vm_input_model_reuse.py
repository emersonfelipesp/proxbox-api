"""Parity contracts for raw and prevalidated VM synchronization inputs."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from proxbox_api.proxmox_to_netbox.models import ProxmoxVmConfigInput, ProxmoxVmResourceInput
from proxbox_api.services.sync.virtual_machines import (
    build_netbox_virtual_machine_payload,
    build_virtual_machine_sync_state_fields,
)
from tests.fixtures import PROXMOX_VM_CONFIG, PROXMOX_VM_RESOURCE


@pytest.mark.parametrize(
    ("resource_overrides", "config_overrides"),
    [
        ({}, {}),
        ({"name": "", "maxcpu": 0, "maxmem": 0, "maxdisk": 0}, {}),
        ({"type": "LXC", "vmid": 2_147_483_647}, {"onboot": "0", "agent": "1"}),
        ({"status": "stopped"}, {"searchdomain": None, "tags": None}),
    ],
)
def test_raw_and_prevalidated_vm_inputs_produce_identical_outputs(
    resource_overrides: dict[str, object],
    config_overrides: dict[str, object],
) -> None:
    resource = {**PROXMOX_VM_RESOURCE, **resource_overrides}
    config = {**PROXMOX_VM_CONFIG, **config_overrides}
    resource_model = ProxmoxVmResourceInput.model_validate(resource)
    config_model = ProxmoxVmConfigInput.model_validate(config)
    payload_kwargs = {
        "cluster_id": 11,
        "device_id": 22,
        "role_id": 33,
        "tag_ids": [5, 7],
        "site_id": 44,
        "tenant_id": 55,
    }
    state_kwargs = {
        "last_updated": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "cluster_name": "cluster-a",
        "proxmox_url": "https://pve.example:8006",
        "endpoint_id": 66,
    }

    assert build_netbox_virtual_machine_payload(
        proxmox_resource=resource,
        proxmox_config=config,
        **payload_kwargs,
    ) == build_netbox_virtual_machine_payload(
        proxmox_resource=resource_model,
        proxmox_config=config_model,
        **payload_kwargs,
    )
    assert build_virtual_machine_sync_state_fields(
        proxmox_resource=resource,
        proxmox_config=config,
        **state_kwargs,
    ) == build_virtual_machine_sync_state_fields(
        proxmox_resource=resource_model,
        proxmox_config=config_model,
        **state_kwargs,
    )


@pytest.mark.parametrize(
    "resource",
    [{}, {"vmid": 101, "name": "vm", "node": "pve01", "type": "not-supported"}],
)
def test_raw_and_prevalidated_resource_validation_errors_match(
    resource: dict[str, object],
) -> None:
    if resource:
        model = ProxmoxVmResourceInput.model_validate(resource)
        assert model.type == "unknown"
        return
    with pytest.raises(ValidationError):
        build_virtual_machine_sync_state_fields(
            proxmox_resource=resource,
            proxmox_config={},
        )
