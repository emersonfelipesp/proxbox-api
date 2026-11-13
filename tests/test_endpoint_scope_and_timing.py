"""Endpoint scoping of estate VM stages, text lookup comparison, and timing logs."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from proxbox_api import netbox_rest
from proxbox_api.services.sync import device_ensure
from proxbox_api.services.sync.guest_vm_interface import _lookup_matches
from proxbox_api.services.sync.stage_timing import log_stage_timing
from proxbox_api.services.sync.vm_helpers import record_id
from proxbox_api.services.sync.vmid_helpers import scope_vms_to_proxmox_sessions


def _vm(netbox_id: int, endpoint_id: int | None) -> dict[str, object]:
    return {
        "id": netbox_id,
        "proxmox_vm_id": 100 + netbox_id,
        "proxmox_endpoint_raw_id": endpoint_id,
    }


def _session(endpoint_id: int | None) -> SimpleNamespace:
    return SimpleNamespace(db_endpoint_id=endpoint_id, name=f"endpoint-{endpoint_id}")


def _messages(capture, level: int) -> list[str]:
    return [record.getMessage() for record in capture.records if record.levelno == level]


def test_scope_drops_only_vms_of_other_endpoints(proxbox_log_capture):
    vms = [_vm(1, 34), _vm(2, 9), _vm(3, 11), _vm(4, 34)]

    scoped = scope_vms_to_proxmox_sessions(vms, [_session(34)], stage="Virtual disk")

    assert [vm["id"] for vm in scoped] == [1, 4]
    info = _messages(proxbox_log_capture, logging.INFO)
    assert any("2 VM(s) of 2 other Proxmox endpoint id(s)" in message for message in info)
    assert _messages(proxbox_log_capture, logging.WARNING) == []


def test_scope_keeps_vms_of_every_selected_endpoint():
    vms = [_vm(1, 34), _vm(2, 9), _vm(3, 11)]

    scoped = scope_vms_to_proxmox_sessions(vms, [_session(34), _session(9)], stage="Snapshot")

    assert [vm["id"] for vm in scoped] == [1, 2]


def test_scope_keeps_a_vm_without_an_endpoint_id():
    vms = [_vm(1, None), _vm(2, 9)]

    scoped = scope_vms_to_proxmox_sessions(vms, [_session(34)], stage="Snapshot")

    assert [vm["id"] for vm in scoped] == [1]


@pytest.mark.parametrize(
    "sessions",
    [[], None, [_session(None)], [_session(34), _session(None)]],
)
def test_scope_is_not_applied_without_proof_of_the_session_endpoints(sessions):
    vms = [_vm(1, 34), _vm(2, 9)]

    assert scope_vms_to_proxmox_sessions(vms, sessions, stage="Snapshot") == vms


@pytest.mark.parametrize("name", ["eth0", "ens18", "lo", "5", ""])
def test_guest_interface_name_lookup_is_compared_as_text_without_warnings(
    name, proxbox_log_capture
):
    lookup = {"virtual_machine": 50, "name": name}

    assert _lookup_matches({"virtual_machine": 50, "name": name}, lookup) is True
    assert _lookup_matches({"virtual_machine": 50, "name": "other0"}, lookup) is False
    assert _messages(proxbox_log_capture, logging.WARNING) == []


def test_guest_lookup_still_rejects_a_foreign_relation():
    lookup = {"guest_interface": 7, "ip_address": 77}

    assert _lookup_matches({"guest_interface": 7, "ip_address": 77}, lookup) is True
    assert _lookup_matches({"guest_interface": 8, "ip_address": 77}, lookup) is False
    assert _lookup_matches({"guest_interface": None, "ip_address": 77}, lookup) is False
    assert (
        _lookup_matches({"virtual_machine": 51, "name": "eth0"}, {"virtual_machine": 50}) is False
    )


@pytest.mark.parametrize("value", ["eth0", b"eth0", 1.5, None])
def test_record_id_of_a_scalar_is_none_without_a_warning(value, proxbox_log_capture):
    assert record_id(value) is None
    assert _messages(proxbox_log_capture, logging.WARNING) == []


def test_record_id_still_reads_ids_from_records():
    assert record_id(7) == 7
    assert record_id("7") == 7
    assert record_id({"id": 7}) == 7
    assert record_id(SimpleNamespace(id=7)) == 7


def test_stage_timing_logs_completed_and_failed_runs(proxbox_log_capture):
    @log_stage_timing("demo-stage")
    async def _ok() -> str:
        return "done"

    @log_stage_timing("demo-stage")
    async def _boom() -> None:
        raise RuntimeError("boom")

    assert asyncio.run(_ok()) == "done"
    with pytest.raises(RuntimeError):
        asyncio.run(_boom())

    info = [m for m in _messages(proxbox_log_capture, logging.INFO) if "demo-stage" in m]
    assert len(info) == 2
    assert "Sync stage demo-stage completed in" in info[0]
    assert "Sync stage demo-stage failed in" in info[1]
    assert "NetBox requests during stage: 0" in info[0]


def test_netbox_request_timing_counts_requests_and_logs_slow_ones(monkeypatch, proxbox_log_capture):
    clock = iter([0.0, 0.2, 10.0, 10.0 + netbox_rest.SLOW_NETBOX_REQUEST_SECONDS])
    monkeypatch.setattr(netbox_rest.time, "perf_counter", lambda: next(clock))

    class _Client:
        async def request(self, method, path, **kwargs):
            return (method, path, kwargs)

    api = SimpleNamespace(client=_Client())
    before = netbox_rest.get_request_timing_totals()

    async def _run():
        first = await netbox_rest._timed_client_request(
            api, "GET", "/api/a/", query={"q": "s3cret"}
        )
        await netbox_rest._timed_client_request(api, "POST", "/api/b/?name=s3cret", payload={})
        return first

    assert asyncio.run(_run()) == ("GET", "/api/a/", {"query": {"q": "s3cret"}})

    after = netbox_rest.get_request_timing_totals()
    assert after[0] - before[0] == 2
    assert after[1] - before[1] == pytest.approx(0.2 + netbox_rest.SLOW_NETBOX_REQUEST_SECONDS)
    assert after[2] - before[2] == 1
    warnings = _messages(proxbox_log_capture, logging.WARNING)
    assert warnings == ["Slow NetBox request: POST /api/b/ took 5.0s"]


def test_netbox_request_timing_is_recorded_when_the_request_fails():
    class _Client:
        async def request(self, method, path, **kwargs):
            raise TimeoutError

    before = netbox_rest.get_request_timing_totals()[0]
    with pytest.raises(TimeoutError):
        asyncio.run(
            netbox_rest._timed_client_request(SimpleNamespace(client=_Client()), "GET", "/api/a/")
        )
    assert netbox_rest.get_request_timing_totals()[0] - before == 1


def test_device_targets_log_the_effective_name_template(caplog):
    caplog.set_level(logging.INFO, logger=device_ensure.logger.name)
    cluster_status = SimpleNamespace(
        name="cluster-a",
        db_endpoint_id=34,
        endpoint_name="endpoint-a",
        node_device_name_template="{node}.bo.example.com",
        node_list=[SimpleNamespace(name="pve01")],
    )

    targets = device_ensure._build_device_targets([cluster_status], cluster_by_name={}, sites={})

    assert [target.desired_name for target in targets] == ["pve01.bo.example.com"]
    info = [record.getMessage() for record in caplog.records]
    assert any(
        "effective node device name template is '{node}.bo.example.com'" in message
        and "endpoint-a" in message
        for message in info
    )
