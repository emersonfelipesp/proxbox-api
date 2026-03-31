"""Regression tests for VM task history synchronization."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from proxbox_api.services.sync.task_history import sync_virtual_machine_task_history


def test_sync_virtual_machine_task_history_builds_human_readable_payload(monkeypatch):
    reconciled: list[tuple[dict, dict]] = []

    async def _fake_rest_reconcile(_nb, _path, lookup, payload, schema, current_normalizer):
        desired_model = schema.model_validate(payload)
        reconciled.append(
            (
                lookup,
                desired_model.model_dump(mode="python", by_alias=True, exclude_none=True),
            )
        )

        class _Record:
            id = 1

        return _Record()

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
        "proxbox_api.services.sync.task_history.rest_reconcile_async",
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
            vm_type="lxc",
            cluster_name="lab",
            tag_refs=[{"name": "Proxbox", "slug": "proxbox"}],
        )
    )

    assert result == 2
    assert reconciled[0][0] == {"upid": "UPID:pve01:1"}
    assert reconciled[0][1]["description"] == "CT 144 - Start"
    assert reconciled[0][1]["status"] == "OK"
    assert reconciled[0][1]["vm_type"] == "lxc"
    assert reconciled[0][1]["start_time"].startswith("2024-03")
