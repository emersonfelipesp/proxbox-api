"""Cloud-init zero-diff behavior for intent update dispatchers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from proxbox_api.routes.intent.cloud_init import CloudInitPayload
from proxbox_api.routes.intent.dispatchers.qemu_update import dispatch_qemu_update
from proxbox_api.routes.intent.schemas import VMIntentPayload


class _FakeConfigEndpoint:
    def __init__(self, fake: "_FakeProxmoxAPI") -> None:
        self._fake = fake

    async def get(self) -> dict[str, object]:
        return dict(self._fake.config)

    async def put(self, **kwargs) -> str:
        self._fake.calls.append(kwargs)
        self._fake.config.update(kwargs)
        return "UPID:pve01:0001:cloud-init"


class _FakeStatusCurrentEndpoint:
    async def get(self) -> dict[str, object]:
        return {"status": "stopped"}


class _FakeStatusEndpoint:
    def __init__(self) -> None:
        self.current = _FakeStatusCurrentEndpoint()


class _FakeVmEndpoint:
    def __init__(self, fake: "_FakeProxmoxAPI") -> None:
        self.config = _FakeConfigEndpoint(fake)
        self.status = _FakeStatusEndpoint()


class _FakeQemuCollection:
    def __init__(self, fake: "_FakeProxmoxAPI") -> None:
        self._fake = fake

    async def get(self) -> list[dict[str, object]]:
        return [{"vmid": 101, "node": "pve01", "type": "qemu", "status": "stopped"}]

    def __call__(self, vmid: int) -> _FakeVmEndpoint:
        assert vmid == 101
        return _FakeVmEndpoint(self._fake)


class _FakeNodeEndpoint:
    def __init__(self, fake: "_FakeProxmoxAPI") -> None:
        self.qemu = _FakeQemuCollection(fake)


class _FakeNodesEndpoint:
    def __init__(self, fake: "_FakeProxmoxAPI") -> None:
        self._fake = fake

    def __call__(self, node: str) -> _FakeNodeEndpoint:
        assert node == "pve01"
        return _FakeNodeEndpoint(self._fake)


class _FakeProxmoxAPI:
    def __init__(self) -> None:
        self.config: dict[str, object] = {"name": "vm-101"}
        self.calls: list[dict[str, object]] = []
        self.nodes = _FakeNodesEndpoint(self)


async def test_qemu_update_cloud_init_second_apply_skips():
    fake = _FakeProxmoxAPI()
    payload = VMIntentPayload(
        vmid=101,
        node="pve01",
        cloud_init=CloudInitPayload(
            user="ubuntu",
            ssh_keys=["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest alice@example"],
            network={"ip": "192.0.2.50", "cidr": 24, "gw": "192.0.2.1"},
        ),
    )
    endpoint_context = SimpleNamespace(session=object(), endpoint_id=1, netbox_id=501)
    journal = AsyncMock(return_value={"id": 1})

    with (
        patch(
            "proxbox_api.routes.intent.dispatchers.qemu_update._gate",
            AsyncMock(return_value=SimpleNamespace(id=1, name="pve01")),
        ),
        patch(
            "proxbox_api.routes.intent.dispatchers.qemu_update._open_proxmox_session",
            AsyncMock(return_value=SimpleNamespace(session=fake)),
        ),
        patch(
            "proxbox_api.routes.intent.dispatchers.qemu_update.write_verb_journal_entry",
            journal,
        ),
        patch(
            "proxbox_api.routes.intent.dispatchers.common.get_netbox_async_session",
            AsyncMock(return_value=object()),
        ),
    ):
        first = await dispatch_qemu_update(endpoint_context, payload, "run-1", actor="alice")
        second = await dispatch_qemu_update(endpoint_context, payload, "run-2", actor="alice")

    assert first.status == "succeeded"
    assert second.status == "skipped"
    assert len(fake.calls) == 1
    assert fake.calls[0]["ciuser"] == "ubuntu"
    assert fake.calls[0]["ipconfig0"] == "ip=192.0.2.50/24,gw=192.0.2.1"
