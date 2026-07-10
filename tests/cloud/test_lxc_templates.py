"""Regression tests for LXC CT-template listing gate behavior.

The template-listing route is read-only and must NOT be blocked by the
``ProxmoxEndpoint.allow_writes`` write gate — that flag only guards mutating
verbs. This mirrors the sibling QEMU ``GET /cloud/vm/templates`` route, which
resolves the endpoint read-only via ``_endpoint_for_read`` and works fine on a
``allow_writes=False`` endpoint. Provisioning (``POST /cloud/lxc/provision``)
is a real write and must keep the write gate.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.routes.cloud import lxc
from proxbox_api.services.cloud_network import AllocatedIPAddress, CloudNetworkConfig


class _FakeSession:
    """Minimal stand-in for the DB session used by the gate resolvers."""

    def __init__(self, endpoint: ProxmoxEndpoint | None) -> None:
        self._endpoint = endpoint

    def get(self, _model: object, _pk: object) -> ProxmoxEndpoint | None:
        return self._endpoint


class _FakeNodes:
    @staticmethod
    def get(*_args: object, **_kwargs: object) -> list[object]:
        return []


class _FakeProxmoxInner:
    nodes = _FakeNodes()


class _FakeProxmox:
    session = _FakeProxmoxInner()

    async def aclose(self) -> None:
        return None


async def test_list_lxc_templates_read_only_ignores_allow_writes(monkeypatch) -> None:
    """A write-disabled but enabled endpoint returns templates, never a 403.

    This is the exact production bug: endpoint had ``allow_writes=False`` and
    the read-only listing 403'd because it used the write gate. On pre-fix code
    this returns a ``JSONResponse`` with status 403 instead of a list.
    """
    endpoint = ProxmoxEndpoint(
        id=16,
        name="pve-10-0-30-71",
        ip_address="10.0.30.71",
        username="root@pam",
        enabled=True,
        allow_writes=False,
    )

    async def _fake_open(passed_endpoint: ProxmoxEndpoint) -> _FakeProxmox:
        assert passed_endpoint is endpoint
        return _FakeProxmox()

    async def _fake_resolve(value: object) -> object:
        return value

    monkeypatch.setattr(lxc, "_open_proxmox_session", _fake_open)
    monkeypatch.setattr(lxc, "resolve_async", _fake_resolve)

    result = await lxc.list_lxc_templates(session=_FakeSession(endpoint), endpoint_id=16)

    assert result == []
    assert not isinstance(result, JSONResponse)


async def test_provision_lxc_still_enforces_write_gate() -> None:
    """The provisioning verb (a real write) must keep the ``allow_writes`` gate."""
    endpoint = ProxmoxEndpoint(
        id=16,
        name="pve-10-0-30-71",
        ip_address="10.0.30.71",
        username="root@pam",
        enabled=True,
        allow_writes=False,
    )
    req = lxc.CloudLXCProvisionRequest(
        endpoint_id=16,
        hostname="ct-test",
        ostemplate="local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst",
        target_node="pve01",
    )

    result = await lxc.provision_lxc(req=req, session=_FakeSession(endpoint), actor=None)

    assert isinstance(result, JSONResponse)
    assert result.status_code == 403


def test_build_lxc_create_params_adds_cloud_network_net0() -> None:
    req = lxc.CloudLXCProvisionRequest(
        endpoint_id=16,
        hostname="ct-test",
        ostemplate="local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst",
        target_node="pve01",
        enforce_cloud_network=True,
    )

    params = lxc._build_lxc_create_params(
        req,
        1001,
        cloud_network=CloudNetworkConfig(
            lock_enabled=True,
            prefix_id=123,
            bridge="vmbr1",
            vlan_tag=2050,
            gateway="168.0.98.1",
        ),
        allocated_ip=AllocatedIPAddress(id=77, address="168.0.98.10", cidr="168.0.98.10/24"),
    )

    assert params["net0"] == "name=eth0,bridge=vmbr1,tag=2050,ip=168.0.98.10/24,gw=168.0.98.1"
