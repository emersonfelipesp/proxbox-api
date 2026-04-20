"""Regression tests for VM task history synchronization."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from proxbox_api.services.sync.task_history import sync_virtual_machine_task_history


def test_sync_virtual_machine_task_history_builds_human_readable_payload(monkeypatch):
    bulk_calls: list[dict[str, object]] = []
    expected_pstart = 2222

    async def _fake_rest_bulk_reconcile(
        _nb,
        _path,
        *,
        payloads,
        lookup_fields,
        schema,
        current_normalizer,
        patchable_fields=None,
    ):
        normalized_payloads = [
            schema.model_validate(payload).model_dump(
                mode="python", by_alias=True, exclude_none=True
            )
            for payload in payloads
        ]
        bulk_calls.append(
            {
                "lookup_fields": lookup_fields,
                "payloads": normalized_payloads,
            }
        )
        assert patchable_fields == {
            "end_time",
            "status",
            "task_state",
            "exitstatus",
            "tags",
            "custom_fields",
        }
        return SimpleNamespace(created=1, updated=0, unchanged=1, failed=0, records=[])

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.dump_models",
        lambda items: items,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_tasks",
        lambda session, node, **kwargs: [
            {
                "upid": f"UPID:{node}:1",
                "node": node,
                "pid": 1111,
                "pstart": 2222,
                "id": "144",
                "type": "vzstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "stopped",
            }
        ],
    )

    def _fake_task_status(session, node, upid):
        return SimpleNamespace(
            model_dump=lambda **kwargs: {
                "upid": upid,
                "node": node,
                "pid": 1111,
                "pstart": 2222,
                "id": "144",
                "type": "vzstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "status": "stopped",
                "exitstatus": "OK",
            }
        )

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_task_status",
        _fake_task_status,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.rest_bulk_reconcile_async",
        _fake_rest_bulk_reconcile,
    )

    result = asyncio.run(
        sync_virtual_machine_task_history(
            netbox_session=object(),
            pxs=[SimpleNamespace(name="lab", session=object())],
            cluster_status=[
                SimpleNamespace(
                    name="lab",
                    node_list=[
                        SimpleNamespace(name="pve01"),
                        SimpleNamespace(name="pve02"),
                    ],
                )
            ],
            virtual_machine_id=144,
            proxmox_vmid=144,
            vm_type="lxc",
            cluster_name="lab",
            tag_refs=[{"name": "Proxbox", "slug": "proxbox"}],
        )
    )

    assert result == 2
    assert len(bulk_calls) == 1
    assert bulk_calls[0]["lookup_fields"] == ["upid"]
    payloads = bulk_calls[0]["payloads"]
    assert isinstance(payloads, list)
    first_payload = payloads[0]
    assert first_payload["upid"] == "UPID:pve01:1"
    assert first_payload["description"] == "CT 144 - Start"
    assert first_payload["status"] == "OK"
    assert first_payload["vm_type"] == "lxc"
    assert first_payload["start_time"].startswith("2024-03")
    assert first_payload["pstart"] == expected_pstart


def test_sync_virtual_machine_task_history_falls_back_to_per_item_on_bulk_failure(monkeypatch):
    reconciled: list[tuple[dict, dict]] = []

    async def _failing_bulk_reconcile(*_args, **_kwargs):
        raise RuntimeError("bulk unavailable")

    async def _fake_rest_reconcile(
        _nb,
        _path,
        lookup,
        payload,
        schema,
        current_normalizer,
        patchable_fields=None,
    ):
        desired_model = schema.model_validate(payload)
        reconciled.append(
            (
                lookup,
                desired_model.model_dump(mode="python", by_alias=True, exclude_none=True),
            )
        )
        assert patchable_fields == {
            "end_time",
            "status",
            "task_state",
            "exitstatus",
            "tags",
            "custom_fields",
        }
        return SimpleNamespace(id=1)

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.dump_models",
        lambda items: items,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_tasks",
        lambda session, node, **kwargs: [
            {
                "upid": f"UPID:{node}:1",
                "node": node,
                "pid": 1111,
                "pstart": 2222,
                "id": "144",
                "type": "vzstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "stopped",
            }
        ],
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_task_status",
        lambda session, node, upid: SimpleNamespace(
            model_dump=lambda **kwargs: {
                "upid": upid,
                "node": node,
                "pid": 1111,
                "pstart": 2222,
                "id": "144",
                "type": "vzstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "status": "stopped",
                "exitstatus": "OK",
            }
        ),
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.rest_bulk_reconcile_async",
        _failing_bulk_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.netbox_rest.rest_reconcile_async",
        _fake_rest_reconcile,
    )

    result = asyncio.run(
        sync_virtual_machine_task_history(
            netbox_session=object(),
            pxs=[SimpleNamespace(name="lab", session=object())],
            cluster_status=[
                SimpleNamespace(
                    name="lab",
                    node_list=[
                        SimpleNamespace(name="pve01"),
                        SimpleNamespace(name="pve02"),
                    ],
                )
            ],
            virtual_machine_id=144,
            proxmox_vmid=144,
            vm_type="lxc",
            cluster_name="lab",
            tag_refs=[{"name": "Proxbox", "slug": "proxbox"}],
        )
    )

    assert result == 2
    assert len(reconciled) == 2
    assert reconciled[0][0] == {"upid": "UPID:pve01:1"}


def test_netbox_task_history_sync_state_accepts_long_exitstatus():
    """Regression test for issue #330: exitstatus/status longer than 255 chars must not raise."""
    from proxbox_api.proxmox_to_netbox.models import NetBoxTaskHistorySyncState

    long_error = "ERROR: " + ("x" * 500)
    model = NetBoxTaskHistorySyncState.model_validate(
        {
            "virtual_machine": 1,
            "upid": "UPID:pve01:00001CB4:0001DA12:69AA4164:qmcreate:101:root@pam:",
            "node": "pve01",
            "task_type": "qmcreate",
            "username": "root@pam",
            "start_time": "2024-03-10T00:00:00",
            "exitstatus": long_error,
            "status": long_error,
        }
    )
    # Both fields must be accepted without validation error
    assert model.exitstatus is not None
    assert model.status is not None
    # Values longer than 255 chars are accepted; truncation kicks in at 2048
    assert len(model.exitstatus) > 255
    assert len(model.status) > 255


def test_netbox_task_history_sync_state_truncates_at_2048():
    """Values exceeding 2048 chars are truncated before reaching NetBox."""
    from proxbox_api.proxmox_to_netbox.models import NetBoxTaskHistorySyncState

    huge_value = "E" * 3000
    model = NetBoxTaskHistorySyncState.model_validate(
        {
            "virtual_machine": 1,
            "upid": "UPID:pve01:00001CB4:0001DA12:69AA4164:qmcreate:101:root@pam:",
            "node": "pve01",
            "task_type": "qmcreate",
            "username": "root@pam",
            "start_time": "2024-03-10T00:00:00",
            "exitstatus": huge_value,
            "status": huge_value,
        }
    )
    assert len(model.exitstatus) == 2048
    assert len(model.status) == 2048
