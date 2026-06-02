"""Tests for the operational-verb allow-writes gate."""

from __future__ import annotations

import json
from typing import Literal

import pytest

from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.routes import proxmox_actions

VmType = Literal["qemu", "lxc"]
Verb = Literal[
    "start",
    "stop",
    "snapshot",
    "migrate",
    "reboot",
    "delete",
    "backup",
    "delete_snapshot",
]

VERB_CASES: list[tuple[Verb, VmType]] = [
    ("start", "qemu"),
    ("start", "lxc"),
    ("stop", "qemu"),
    ("stop", "lxc"),
    ("snapshot", "qemu"),
    ("snapshot", "lxc"),
    ("migrate", "qemu"),
    ("migrate", "lxc"),
    ("reboot", "qemu"),
    ("reboot", "lxc"),
    ("delete", "qemu"),
    ("delete", "lxc"),
    ("backup", "qemu"),
    ("backup", "lxc"),
    ("delete_snapshot", "qemu"),
    ("delete_snapshot", "lxc"),
]

LIFECYCLE_VERBS = {"reboot", "delete", "backup", "delete_snapshot"}


class _GateSession:
    def __init__(self, endpoint: ProxmoxEndpoint | None = None) -> None:
        self.endpoint = endpoint

    async def get(self, model: object, object_id: int) -> ProxmoxEndpoint | None:
        if model is ProxmoxEndpoint and self.endpoint is not None and object_id == self.endpoint.id:
            return self.endpoint
        return None


def _endpoint(*, allow_writes: bool) -> ProxmoxEndpoint:
    return ProxmoxEndpoint(
        id=73,
        name="pve-test",
        ip_address="10.0.0.10",
        port=8006,
        username="root@pam",
        verify_ssl=False,
        allow_writes=allow_writes,
    )


def _json_response(response) -> dict[str, object]:
    return json.loads(response.body)


async def _invoke(
    verb: Verb,
    vm_type: VmType,
    session: _GateSession,
    endpoint_id: int | None,
):
    if verb == "start":
        return await proxmox_actions._handle_start(
            vm_type,
            100,
            session,
            endpoint_id,
            None,
            "pytest",  # type: ignore[arg-type]
        )
    if verb == "stop":
        return await proxmox_actions._handle_stop(
            vm_type,
            100,
            session,
            endpoint_id,
            None,
            "pytest",  # type: ignore[arg-type]
        )
    if verb == "snapshot":
        return await proxmox_actions._handle_snapshot(
            vm_type,
            100,
            session,  # type: ignore[arg-type]
            endpoint_id,
            None,
            "pytest",
            proxmox_actions.SnapshotRequest(snapname="pre-upgrade"),
        )
    if verb == "migrate":
        return await proxmox_actions._handle_migrate(
            vm_type,
            100,
            session,  # type: ignore[arg-type]
            endpoint_id,
            None,
            "pytest",
            proxmox_actions.MigrateRequest(target="pve-node-02"),
        )
    if verb == "reboot":
        return await proxmox_actions._handle_reboot(
            vm_type,
            100,
            session,
            endpoint_id,
            None,
            "pytest",  # type: ignore[arg-type]
        )
    if verb == "delete":
        return await proxmox_actions._handle_delete(
            vm_type,
            100,
            session,
            endpoint_id,
            None,
            "pytest",  # type: ignore[arg-type]
        )
    if verb == "backup":
        return await proxmox_actions._handle_backup(
            vm_type,
            100,
            session,  # type: ignore[arg-type]
            endpoint_id,
            None,
            "pytest",
            proxmox_actions.BackupRequest(storage="pbs-main"),
        )
    return await proxmox_actions._handle_delete_snapshot(
        vm_type,
        100,
        "pre-upgrade",
        session,  # type: ignore[arg-type]
        endpoint_id,
        None,
        "pytest",
    )


@pytest.mark.parametrize(("verb", "vm_type"), VERB_CASES)
@pytest.mark.asyncio
async def test_missing_endpoint_id_returns_403(verb: Verb, vm_type: VmType):
    resp = await _invoke(verb, vm_type, _GateSession(), None)
    assert resp.status_code == 403
    body = _json_response(resp)
    assert body["reason"] == "endpoint_id_required"


@pytest.mark.parametrize(("verb", "vm_type"), VERB_CASES)
@pytest.mark.asyncio
async def test_unknown_endpoint_id_returns_403(verb: Verb, vm_type: VmType):
    resp = await _invoke(verb, vm_type, _GateSession(), 999)
    assert resp.status_code == 403
    body = _json_response(resp)
    assert body["reason"] == "endpoint_not_found"


@pytest.mark.parametrize(("verb", "vm_type"), VERB_CASES)
@pytest.mark.asyncio
async def test_endpoint_with_writes_disabled_returns_403(verb: Verb, vm_type: VmType):
    endpoint = _endpoint(allow_writes=False)
    resp = await _invoke(verb, vm_type, _GateSession(endpoint), endpoint.id)
    assert resp.status_code == 403
    body = _json_response(resp)
    expected_reason = (
        "writes_disabled_for_endpoint" if verb in LIFECYCLE_VERBS else "endpoint_writes_disabled"
    )
    assert body["reason"] == expected_reason
    assert body["endpoint_id"] == endpoint.id


def test_lifecycle_routes_are_registered_with_fixed_contract_paths():
    mounted_prefix = "/proxmox"
    actual = {
        (method, f"{mounted_prefix}{route.path}")
        for route in proxmox_actions.router.routes
        for method in getattr(route, "methods", set())
    }
    assert ("POST", "/proxmox/qemu/{vmid}/reboot") in actual
    assert ("POST", "/proxmox/lxc/{vmid}/reboot") in actual
    assert ("DELETE", "/proxmox/qemu/{vmid}") in actual
    assert ("DELETE", "/proxmox/lxc/{vmid}") in actual
    assert ("POST", "/proxmox/qemu/{vmid}/backup") in actual
    assert ("POST", "/proxmox/lxc/{vmid}/backup") in actual
    assert ("DELETE", "/proxmox/qemu/{vmid}/snapshot/{snapname}") in actual
    assert ("DELETE", "/proxmox/lxc/{vmid}/snapshot/{snapname}") in actual


def test_allow_writes_field_defaults_to_false():
    """The SQLModel default for allow_writes is False (gate closed by default)."""
    assert ProxmoxEndpoint.model_fields["allow_writes"].default is False
