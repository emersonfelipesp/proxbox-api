"""Regression tests for the VM-sync vcpus=0 NetBox 400.

Right after a clone the new VM is not yet in Proxmox /cluster/resources, so the
resource ``maxcpu`` is 0. NetBox rejects vcpus=0 (DecimalField MinValueValidator
0.01; null is allowed). `_build_netbox_vm_payload` must derive vcpus from the VM
config (cores*sockets) and never emit 0, and the create-body model must accept a
null vcpus and coerce 0 -> None.
"""

from datetime import datetime, timezone

from proxbox_api.proxmox_to_netbox.models import NetBoxVirtualMachineCreateBody
from proxbox_api.services.sync.individual.vm_sync import _build_netbox_vm_payload

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _payload(resource: dict, config: dict) -> dict:
    return _build_netbox_vm_payload(
        resource=resource,
        config=config,
        cluster_id=1,
        device_id=None,
        role_id=None,
        tag_ids=[],
        last_updated=_NOW,
    )


def test_vcpus_derived_from_config_when_resource_maxcpu_zero():
    # Freshly cloned VM: empty resource, config has cores/sockets.
    payload = _payload({"type": "qemu", "vmid": 123}, {"cores": 2, "sockets": 2})
    assert payload["vcpus"] == 4  # cores * sockets
    assert payload["vcpus"] != 0


def test_vcpus_prefers_resource_maxcpu_when_present():
    payload = _payload({"type": "qemu", "vmid": 123, "maxcpu": 8}, {"cores": 2, "sockets": 1})
    assert payload["vcpus"] == 8


def test_vcpus_none_when_no_cpu_info_anywhere():
    payload = _payload({"type": "qemu", "vmid": 123}, {})
    assert payload["vcpus"] is None  # null is valid in NetBox; 0 is not


def test_create_body_coerces_zero_vcpus_to_none():
    body = NetBoxVirtualMachineCreateBody(name="dns01", status="active", vcpus=0)
    assert body.vcpus is None


def test_create_body_accepts_null_vcpus():
    body = NetBoxVirtualMachineCreateBody(name="dns01", status="active", vcpus=None)
    assert body.vcpus is None


def test_create_body_keeps_positive_vcpus():
    body = NetBoxVirtualMachineCreateBody(name="dns01", status="active", vcpus=4)
    assert body.vcpus == 4
