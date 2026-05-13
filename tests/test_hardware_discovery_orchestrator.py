"""Orchestrator tests: sequential dispatch, SSE shapes, failure → warning frames.

Patches :class:`proxmox_sdk.ssh.RemoteSSHClient` and
``proxmox_sdk.node.hardware.discover_node`` with deterministic fakes that yield
fixture facts. Verifies the orchestrator:

- dispatches nodes sequentially in the order received,
- emits one ``hardware_discovery`` SSE frame per success,
- maps every documented exception shape to an ``item_progress`` warning frame,
- skips reflect when the success path raises.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from proxbox_api.services import hardware_discovery


# Fake fact shapes — duck-type the proxmox_sdk dataclasses the orchestrator reads.
@dataclass
class FakeSystem:
    product_name: str | None = "PowerEdge R740"


@dataclass
class FakeChassis:
    serial_number: str | None = "SN12345"
    manufacturer: str | None = "Dell Inc."


@dataclass
class FakeEthtool:
    duplex: str | None = "full"
    link_detected: bool | None = True


@dataclass
class FakeNic:
    name: str
    speed_gbps: float | None = 10.0
    ethtool: FakeEthtool | None = field(default_factory=FakeEthtool)


@dataclass
class FakeFacts:
    system: FakeSystem = field(default_factory=FakeSystem)
    chassis: FakeChassis = field(default_factory=FakeChassis)
    nics: tuple[FakeNic, ...] = field(default_factory=tuple)


class _FakeBridge:
    def __init__(self) -> None:
        self.frames: list[tuple[str, dict[str, Any]]] = []

    async def emit_hardware_discovery_progress(self, **payload: Any) -> None:
        self.frames.append(("hardware_discovery", dict(payload)))

    async def emit_item_progress(self, **payload: Any) -> None:
        self.frames.append(("item_progress", dict(payload)))


class _FakeSSHClient:
    instances: list["_FakeSSHClient"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        _FakeSSHClient.instances.append(self)

    async def __aenter__(self) -> "_FakeSSHClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


def _install_fake_modules(
    monkeypatch: pytest.MonkeyPatch,
    *,
    discover_result: Any = None,
    discover_raise: type[BaseException] | None = None,
    ssh_raise_on_enter: type[BaseException] | None = None,
) -> dict[str, Any]:
    """Install fake ``proxmox_sdk.ssh`` + ``proxmox_sdk.node.hardware`` modules."""
    HostKeyMismatch = type("HostKeyMismatch", (Exception,), {})
    SshTimeout = type("SshTimeout", (Exception,), {})
    SshAuthFailed = type("SshAuthFailed", (Exception,), {})
    CommandNotAllowed = type("CommandNotAllowed", (Exception,), {})
    OutputTooLarge = type("OutputTooLarge", (Exception,), {})

    exceptions = {
        "HostKeyMismatch": HostKeyMismatch,
        "SshTimeout": SshTimeout,
        "SshAuthFailed": SshAuthFailed,
        "CommandNotAllowed": CommandNotAllowed,
        "OutputTooLarge": OutputTooLarge,
    }

    _FakeSSHClient.instances = []

    class _RaisingClient(_FakeSSHClient):
        async def __aenter__(self) -> "_RaisingClient":
            assert ssh_raise_on_enter is not None
            raise ssh_raise_on_enter("simulated")

    ClientCls: type = _RaisingClient if ssh_raise_on_enter else _FakeSSHClient

    fake_ssh = types.ModuleType("proxmox_sdk.ssh")
    fake_ssh.RemoteSSHClient = ClientCls  # type: ignore[attr-defined]
    for name, cls in exceptions.items():
        setattr(fake_ssh, name, cls)
    monkeypatch.setitem(sys.modules, "proxmox_sdk.ssh", fake_ssh)

    fake_hw = types.ModuleType("proxmox_sdk.node.hardware")

    async def _discover(_client: Any) -> Any:
        if discover_raise is not None:
            raise discover_raise("simulated")
        return discover_result

    fake_hw.discover_node = _discover  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "proxmox_sdk.node.hardware", fake_hw)

    return exceptions


@pytest.fixture
def patch_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardware_discovery, "is_enabled", lambda: True)


@pytest.fixture
def patch_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_fetch(_session: Any, node_id: int, host: str, **_kw: Any) -> Any:
        return hardware_discovery.NodeSSHCredential(
            node_id=node_id,
            host=host,
            username="proxbox-discovery",
            known_host_fingerprint="SHA256:" + "A" * 43,
            private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----",
            password=None,
        )

    monkeypatch.setattr(hardware_discovery, "fetch_credential", _fake_fetch)


@pytest.fixture
def patch_reflect(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    reflect = AsyncMock()
    monkeypatch.setattr(hardware_discovery, "reflect_to_netbox", reflect)
    return reflect


@pytest.mark.asyncio
async def test_success_emits_hardware_discovery_frame(
    monkeypatch: pytest.MonkeyPatch,
    patch_enabled: None,
    patch_credential: None,
    patch_reflect: AsyncMock,
) -> None:
    facts = FakeFacts(nics=(FakeNic(name="eno1"), FakeNic(name="eno2")))
    _install_fake_modules(monkeypatch, discover_result=facts)
    bridge = _FakeBridge()

    await hardware_discovery.run_for_nodes(
        netbox_session=MagicMock(),
        nodes=[{"id": 1, "name": "node-a", "host": "10.0.0.1", "cluster": "edge"}],
        bridge=bridge,
    )

    assert patch_reflect.await_count == 1
    assert len(bridge.frames) == 1
    kind, payload = bridge.frames[0]
    assert kind == "hardware_discovery"
    assert payload["node"] == "node-a"
    assert payload["cluster"] == "edge"
    assert payload["chassis_serial"] == "SN12345"
    assert payload["chassis_manufacturer"] == "Dell Inc."
    assert payload["chassis_product"] == "PowerEdge R740"
    assert payload["nic_count"] == 2


@pytest.mark.asyncio
async def test_sequential_dispatch_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
    patch_enabled: None,
    patch_credential: None,
    patch_reflect: AsyncMock,
) -> None:
    _install_fake_modules(monkeypatch, discover_result=FakeFacts())
    bridge = _FakeBridge()

    await hardware_discovery.run_for_nodes(
        netbox_session=MagicMock(),
        nodes=[
            {"id": 1, "name": "node-a", "host": "10.0.0.1"},
            {"id": 2, "name": "node-b", "host": "10.0.0.2"},
            {"id": 3, "name": "node-c", "host": "10.0.0.3"},
        ],
        bridge=bridge,
    )

    hosts_in_order = [c.kwargs["host"] for c in _FakeSSHClient.instances]
    assert hosts_in_order == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    assert [f[1]["node"] for f in bridge.frames] == ["node-a", "node-b", "node-c"]


@pytest.mark.asyncio
async def test_host_key_mismatch_emits_warning(
    monkeypatch: pytest.MonkeyPatch,
    patch_enabled: None,
    patch_credential: None,
    patch_reflect: AsyncMock,
) -> None:
    # Install the fake module ONCE with a forward reference: pre-build the
    # exception classes, then have the SSH client raise the same class instance
    # the orchestrator imports — otherwise the except-clause sees a different
    # class than the one raised and falls through to generic Exception.
    HostKeyMismatch = type("HostKeyMismatch", (Exception,), {})
    SshTimeout = type("SshTimeout", (Exception,), {})
    SshAuthFailed = type("SshAuthFailed", (Exception,), {})
    CommandNotAllowed = type("CommandNotAllowed", (Exception,), {})
    OutputTooLarge = type("OutputTooLarge", (Exception,), {})
    _FakeSSHClient.instances = []

    class _Raising(_FakeSSHClient):
        async def __aenter__(self) -> "_Raising":
            raise HostKeyMismatch("simulated")

    fake_ssh = types.ModuleType("proxmox_sdk.ssh")
    fake_ssh.RemoteSSHClient = _Raising  # type: ignore[attr-defined]
    fake_ssh.HostKeyMismatch = HostKeyMismatch  # type: ignore[attr-defined]
    fake_ssh.SshTimeout = SshTimeout  # type: ignore[attr-defined]
    fake_ssh.SshAuthFailed = SshAuthFailed  # type: ignore[attr-defined]
    fake_ssh.CommandNotAllowed = CommandNotAllowed  # type: ignore[attr-defined]
    fake_ssh.OutputTooLarge = OutputTooLarge  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "proxmox_sdk.ssh", fake_ssh)

    fake_hw = types.ModuleType("proxmox_sdk.node.hardware")

    async def _discover(_c: Any) -> Any:
        return FakeFacts()

    fake_hw.discover_node = _discover  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "proxmox_sdk.node.hardware", fake_hw)

    bridge = _FakeBridge()
    await hardware_discovery.run_for_nodes(
        netbox_session=MagicMock(),
        nodes=[{"id": 1, "name": "node-a", "host": "10.0.0.1"}],
        bridge=bridge,
    )

    patch_reflect.assert_not_called()
    assert bridge.frames[-1][0] == "item_progress"
    assert bridge.frames[-1][1]["warning"] == "host_key_mismatch"


def _install_with_discover_raise(
    monkeypatch: pytest.MonkeyPatch, exc_name: str
) -> type[BaseException]:
    HostKeyMismatch = type("HostKeyMismatch", (Exception,), {})
    SshTimeout = type("SshTimeout", (Exception,), {})
    SshAuthFailed = type("SshAuthFailed", (Exception,), {})
    CommandNotAllowed = type("CommandNotAllowed", (Exception,), {})
    OutputTooLarge = type("OutputTooLarge", (Exception,), {})
    classes = {
        "HostKeyMismatch": HostKeyMismatch,
        "SshTimeout": SshTimeout,
        "SshAuthFailed": SshAuthFailed,
        "CommandNotAllowed": CommandNotAllowed,
        "OutputTooLarge": OutputTooLarge,
    }
    raise_cls = classes[exc_name]

    fake_ssh = types.ModuleType("proxmox_sdk.ssh")
    fake_ssh.RemoteSSHClient = _FakeSSHClient  # type: ignore[attr-defined]
    for name, cls in classes.items():
        setattr(fake_ssh, name, cls)
    monkeypatch.setitem(sys.modules, "proxmox_sdk.ssh", fake_ssh)

    fake_hw = types.ModuleType("proxmox_sdk.node.hardware")

    async def _discover(_c: Any) -> Any:
        raise raise_cls("simulated")

    fake_hw.discover_node = _discover  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "proxmox_sdk.node.hardware", fake_hw)
    _FakeSSHClient.instances = []
    return raise_cls


@pytest.mark.asyncio
async def test_timeout_emits_warning(
    monkeypatch: pytest.MonkeyPatch,
    patch_enabled: None,
    patch_credential: None,
    patch_reflect: AsyncMock,
) -> None:
    _install_with_discover_raise(monkeypatch, "SshTimeout")
    bridge = _FakeBridge()

    await hardware_discovery.run_for_nodes(
        netbox_session=MagicMock(),
        nodes=[{"id": 1, "name": "node-a", "host": "10.0.0.1"}],
        bridge=bridge,
    )

    patch_reflect.assert_not_called()
    assert bridge.frames[-1][1]["warning"] == "hardware_discovery_timeout"


@pytest.mark.asyncio
async def test_auth_failed_emits_warning(
    monkeypatch: pytest.MonkeyPatch,
    patch_enabled: None,
    patch_credential: None,
    patch_reflect: AsyncMock,
) -> None:
    _install_with_discover_raise(monkeypatch, "SshAuthFailed")
    bridge = _FakeBridge()

    await hardware_discovery.run_for_nodes(
        netbox_session=MagicMock(),
        nodes=[{"id": 1, "name": "node-a", "host": "10.0.0.1"}],
        bridge=bridge,
    )

    patch_reflect.assert_not_called()
    assert bridge.frames[-1][1]["warning"] == "hardware_discovery_auth_failed"


@pytest.mark.asyncio
async def test_missing_credential_skips_node(
    monkeypatch: pytest.MonkeyPatch,
    patch_enabled: None,
    patch_reflect: AsyncMock,
) -> None:
    _install_fake_modules(monkeypatch, discover_result=FakeFacts())

    def _raise(*_a: Any, **_kw: Any) -> None:
        raise hardware_discovery.MissingCredential("no credential")

    monkeypatch.setattr(hardware_discovery, "fetch_credential", _raise)
    bridge = _FakeBridge()

    await hardware_discovery.run_for_nodes(
        netbox_session=MagicMock(),
        nodes=[{"id": 1, "name": "node-a", "host": "10.0.0.1"}],
        bridge=bridge,
    )

    patch_reflect.assert_not_called()
    assert bridge.frames[-1][1]["warning"] == "hardware_discovery_no_credential"


@pytest.mark.asyncio
async def test_missing_host_skips_node(
    monkeypatch: pytest.MonkeyPatch,
    patch_enabled: None,
    patch_reflect: AsyncMock,
) -> None:
    _install_fake_modules(monkeypatch, discover_result=FakeFacts())
    bridge = _FakeBridge()

    await hardware_discovery.run_for_nodes(
        netbox_session=MagicMock(),
        nodes=[{"id": 1, "name": "node-a", "host": ""}],
        bridge=bridge,
    )

    patch_reflect.assert_not_called()
    assert bridge.frames[-1][1]["warning"] == "hardware_discovery_no_primary_ip"
