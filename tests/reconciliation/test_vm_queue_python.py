"""Python contract tests for VM reconciliation queue behavior."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.proxmox_to_netbox.models import ProxmoxVmConfigInput
from proxbox_api.services.sync.reconciliation.types import NetBoxVMOperation, PreparedVMState
from proxbox_api.services.sync.reconciliation.vm_queue import build_vm_operation_queue_python


def _prepared_vm(
    *,
    cluster_name: str = "cluster-a",
    cluster: object = 1,
    vmid: int = 100,
    vm_type: str = "qemu",
    name: str | None = None,
    memory: object = 2048,
    tags: list[object] | None = None,
    custom_fields: dict[str, object] | None = None,
    endpoint_id: int = 500,
    role: object = 20,
    description: str | None = "Synced from Proxmox node pve01",
) -> PreparedVMState:
    desired_payload = {
        "name": name or f"{vm_type}-{vmid}",
        "status": "active",
        "cluster": cluster,
        "device": 10,
        "role": role,
        "vcpus": 2,
        "memory": memory,
        "disk": 30,
        "tags": tags if tags is not None else [99],
        "custom_fields": (
            custom_fields
            if custom_fields is not None
            else {
                "proxmox_endpoint_id": endpoint_id,
                "proxmox_vm_id": vmid,
                "proxmox_vm_type": vm_type,
            }
        ),
        "description": description,
    }
    return PreparedVMState(
        cluster_name=cluster_name,
        resource={"name": desired_payload["name"], "vmid": vmid, "type": vm_type},
        vm_config={},
        vm_config_obj=ProxmoxVmConfigInput.model_validate({}),
        desired_payload=desired_payload,
        lookup={"cf_proxmox_vm_id": vmid, "cf_proxmox_endpoint_id": endpoint_id},
        now=datetime.now(timezone.utc),
        vm_type=vm_type,
    )


def _snapshot_vm(
    *,
    record_id: int = 2000,
    cluster: object = 1,
    vmid: object | None = 100,
    vm_type: object | None = "qemu",
    name: str = "qemu-100",
    memory: object = 2048,
    tags: list[object] | None = None,
    custom_fields: dict[str, object] | None = None,
    endpoint_id: int = 500,
    role: object = 20,
    description: str | None = "Synced from Proxmox node pve01",
    include_virtual_machine_type: bool = True,
    virtual_machine_type: object = 55,
) -> dict[str, object]:
    if custom_fields is None:
        custom_fields = {}
        custom_fields["proxmox_endpoint_id"] = endpoint_id
        if vmid is not None:
            custom_fields["proxmox_vm_id"] = vmid
        if vm_type is not None:
            custom_fields["proxmox_vm_type"] = vm_type

    record: dict[str, object] = {
        "id": record_id,
        "name": name,
        "status": "active",
        "cluster": cluster if isinstance(cluster, dict) else {"id": cluster},
        "device": {"id": 10},
        "role": {"id": role} if isinstance(role, int) else role,
        "vcpus": 2,
        "memory": memory,
        "disk": 30,
        "tags": tags if tags is not None else [{"id": 99}],
        "custom_fields": custom_fields,
        "description": description,
    }
    if include_virtual_machine_type:
        record["virtual_machine_type"] = (
            {"id": virtual_machine_type}
            if isinstance(virtual_machine_type, int)
            else virtual_machine_type
        )
    return record


def _queue(
    prepared: list[PreparedVMState],
    snapshot: list[dict[str, object]],
    **flags: bool,
) -> list[NetBoxVMOperation]:
    return build_vm_operation_queue_python(prepared, snapshot, **flags)


def test_missing_cluster_in_desired_payload_matches_by_endpoint() -> None:
    prepared = [_prepared_vm(cluster=None)]

    queue = _queue(prepared, [_snapshot_vm()])

    assert [op.method for op in queue] == ["GET"]


def test_missing_proxmox_vmid_in_netbox_record_creates() -> None:
    prepared = [_prepared_vm(vmid=101)]
    snapshot = [_snapshot_vm(record_id=2101, vmid=None, name="qemu-101")]

    queue = _queue(prepared, snapshot)

    assert [op.method for op in queue] == ["CREATE"]


def test_existing_role_is_not_patched_when_overwrite_role_false() -> None:
    prepared = [_prepared_vm(vmid=102, role=21)]
    snapshot = [_snapshot_vm(record_id=2102, vmid=102, role=20, name="qemu-102")]

    queue = _queue(prepared, snapshot, overwrite_vm_role=False)

    assert [op.method for op in queue] == ["GET"]
    assert "role" not in queue[0].patch_payload


def test_existing_tags_are_merged_when_overwrite_tags_true() -> None:
    prepared = [_prepared_vm(vmid=103, tags=[99])]
    snapshot = [_snapshot_vm(record_id=2103, vmid=103, name="qemu-103", tags=[{"id": 77}])]

    queue = _queue(prepared, snapshot, overwrite_vm_tags=True)

    assert [op.method for op in queue] == ["UPDATE"]
    assert queue[0].patch_payload["tags"] == [77, 99]


def test_existing_tags_are_preserved_when_overwrite_tags_false() -> None:
    prepared = [_prepared_vm(vmid=104, tags=[99])]
    snapshot = [_snapshot_vm(record_id=2104, vmid=104, name="qemu-104", tags=[{"id": 77}])]

    queue = _queue(prepared, snapshot, overwrite_vm_tags=False)

    assert [op.method for op in queue] == ["GET"]
    assert "tags" not in queue[0].patch_payload


def test_existing_description_is_not_patched_when_overwrite_description_false() -> None:
    prepared = [_prepared_vm(vmid=105, description="Desired description")]
    snapshot = [
        _snapshot_vm(record_id=2105, vmid=105, name="qemu-105", description="Operator description")
    ]

    queue = _queue(prepared, snapshot, overwrite_vm_description=False)

    assert [op.method for op in queue] == ["GET"]
    assert "description" not in queue[0].patch_payload


def test_existing_custom_fields_are_not_patched_when_overwrite_custom_fields_false() -> None:
    prepared = [
        _prepared_vm(
            vmid=106,
            custom_fields={"proxmox_vm_id": 106, "proxmox_vm_type": "qemu", "foo": "desired"},
        )
    ]
    snapshot = [
        _snapshot_vm(
            record_id=2106,
            vmid=106,
            name="qemu-106",
            custom_fields={"proxmox_vm_id": 106, "proxmox_vm_type": "qemu", "foo": "operator"},
        )
    ]

    queue = _queue(prepared, snapshot, overwrite_vm_custom_fields=False)

    assert [op.method for op in queue] == ["GET"]
    assert "custom_fields" not in queue[0].patch_payload


def test_virtual_machine_type_is_not_patched_when_netbox_lacks_field() -> None:
    prepared = [_prepared_vm(vmid=107)]
    prepared[0].desired_payload["virtual_machine_type"] = 55
    snapshot = [
        _snapshot_vm(
            record_id=2107,
            vmid=107,
            name="qemu-107",
            include_virtual_machine_type=False,
        )
    ]

    queue = _queue(prepared, snapshot, supports_virtual_machine_type_field=False)

    assert [op.method for op in queue] == ["GET"]
    assert "virtual_machine_type" not in queue[0].patch_payload


def test_operation_queue_order_is_deterministic() -> None:
    prepared = [
        _prepared_vm(vmid=108, name="qemu-108"),
        _prepared_vm(vmid=109, name="qemu-109"),
        _prepared_vm(vmid=110, name="qemu-110"),
    ]
    snapshot = [
        _snapshot_vm(record_id=2109, vmid=109, name="qemu-109"),
        _snapshot_vm(record_id=2108, vmid=108, name="qemu-108"),
    ]

    first = _queue(prepared, snapshot)
    second = _queue(prepared, snapshot)

    assert [(op.method, op.prepared.resource["vmid"]) for op in first] == [
        ("GET", 108),
        ("GET", 109),
        ("CREATE", 110),
    ]
    assert [(op.method, op.prepared.resource["vmid"]) for op in second] == [
        (op.method, op.prepared.resource["vmid"]) for op in first
    ]


def test_qemu_vm_and_lxc_container_with_same_vmid_in_same_cluster_do_not_collide() -> None:
    prepared = [
        _prepared_vm(vmid=100, vm_type="qemu", name="qemu-100"),
        _prepared_vm(vmid=100, vm_type="lxc", name="lxc-100"),
    ]
    snapshot = [
        _snapshot_vm(record_id=3001, vmid=100, vm_type="qemu", name="qemu-100"),
        _snapshot_vm(record_id=3002, vmid=100, vm_type="lxc", name="lxc-100"),
    ]

    queue = _queue(prepared, snapshot)

    assert [op.method for op in queue] == ["GET", "GET"]
    assert [op.existing_record["id"] for op in queue if op.existing_record] == [3001, 3002]


def test_same_vmid_on_different_endpoints_does_not_collide() -> None:
    prepared = [
        _prepared_vm(vmid=100, endpoint_id=1, name="qemu-100"),
        _prepared_vm(vmid=100, endpoint_id=2, name="qemu-100"),
    ]
    snapshot = [
        _snapshot_vm(record_id=4001, vmid=100, endpoint_id=1, name="qemu-100"),
        _snapshot_vm(record_id=4002, vmid=100, endpoint_id=2, name="qemu-100"),
    ]

    queue = _queue(prepared, snapshot)

    assert [op.method for op in queue] == ["GET", "GET"]
    assert [op.existing_record["id"] for op in queue if op.existing_record] == [4001, 4002]


def test_int_and_float_memory_values_do_not_create_spurious_diff() -> None:
    prepared = [_prepared_vm(vmid=111, memory=2048)]
    snapshot = [_snapshot_vm(record_id=2111, vmid=111, name="qemu-111", memory=2048.0)]

    queue = _queue(prepared, snapshot)

    assert [op.method for op in queue] == ["GET"]


def test_tag_comparison_is_order_independent() -> None:
    prepared = [_prepared_vm(vmid=112, tags=[1, 2, 3])]
    snapshot = [_snapshot_vm(record_id=2112, vmid=112, name="qemu-112", tags=[{"id": 3}, 1, 2])]

    queue = _queue(prepared, snapshot)

    assert [op.method for op in queue] == ["GET"]


def test_custom_field_null_and_missing_are_different() -> None:
    prepared = [_prepared_vm(vmid=113, custom_fields={"proxmox_vm_id": 113, "foo": None})]
    snapshot = [
        _snapshot_vm(
            record_id=2113,
            vmid=113,
            vm_type=None,
            name="qemu-113",
            custom_fields={"proxmox_vm_id": 113},
        )
    ]

    queue = _queue(prepared, snapshot)

    assert [op.method for op in queue] == ["UPDATE"]
    assert queue[0].patch_payload["custom_fields"] == {"proxmox_vm_id": 113, "foo": None}


def test_relation_as_int_and_nested_object_compare_equal() -> None:
    prepared = [_prepared_vm(vmid=114, cluster=1)]
    snapshot = [_snapshot_vm(record_id=2114, vmid=114, name="qemu-114", cluster={"id": 1})]

    queue = _queue(prepared, snapshot)

    assert [op.method for op in queue] == ["GET"]


def test_untyped_snapshot_record_matches_only_when_unambiguous() -> None:
    prepared = [_prepared_vm(vmid=115, vm_type="qemu", name="qemu-115")]
    snapshot = [_snapshot_vm(record_id=2115, vmid=115, vm_type=None, name="qemu-115")]

    queue = _queue(prepared, snapshot)

    assert [op.method for op in queue] == ["UPDATE"]
    assert queue[0].existing_record and queue[0].existing_record["id"] == 2115
    assert queue[0].patch_payload["custom_fields"] == {
        "proxmox_endpoint_id": 500,
        "proxmox_vm_id": 115,
        "proxmox_vm_type": "qemu",
    }


def test_untyped_ambiguous_snapshot_records_create_instead_of_guessing() -> None:
    prepared = [_prepared_vm(vmid=116, vm_type="qemu", name="qemu-116")]
    snapshot = [
        _snapshot_vm(record_id=2116, vmid=116, vm_type=None, name="operator-qemu-116"),
        _snapshot_vm(record_id=2117, vmid=116, vm_type="lxc", name="lxc-116"),
    ]

    queue = _queue(prepared, snapshot)

    assert [op.method for op in queue] == ["CREATE"]
