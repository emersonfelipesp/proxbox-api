"""Tests for operational Proxmox config tag mutation routes."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Literal

import pytest

from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.routes import proxmox_tags

VmType = Literal["qemu", "lxc"]


class _GateSession:
    def __init__(self, endpoint: ProxmoxEndpoint | None = None) -> None:
        self.endpoint = endpoint

    async def get(self, model: object, object_id: int) -> ProxmoxEndpoint | None:
        if model is ProxmoxEndpoint and self.endpoint is not None and object_id == self.endpoint.id:
            return self.endpoint
        return None


class _FakeConfig:
    def __init__(self, tags: str) -> None:
        self.tags = tags

    async def get(self) -> _FakeConfig:
        return self

    async def put(self, **kwargs: object) -> None:
        if "tags" in kwargs:
            self.tags = str(kwargs["tags"])


class _FakeProxy:
    def __init__(self, tags: str) -> None:
        self.config = _FakeConfig(tags)


class _FakeProxmox:
    def __init__(self, tags: str) -> None:
        self._tags = tags

    def nodes(self, node: str) -> SimpleNamespace:
        del node
        return SimpleNamespace(
            qemu=lambda vmid: _FakeProxy(self._tags) if vmid == 100 else None,
            lxc=lambda vmid: _FakeProxy(self._tags) if vmid == 100 else None,
        )


def _endpoint(*, allow_writes: bool = True) -> ProxmoxEndpoint:
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


@pytest.mark.asyncio
async def test_replace_tags_requires_endpoint_id(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _GateSession(_endpoint())
    response = await proxmox_tags.replace_proxmox_tags(
        "qemu",
        100,
        proxmox_tags.ReplaceProxmoxTagsBody(node="pve1", tags=["alpha"]),
        session,  # type: ignore[arg-type]
        endpoint_id=None,
        actor="pytest",
    )
    payload = _json_response(response)
    assert payload["reason"] == "endpoint_id_required"


@pytest.mark.asyncio
async def test_replace_tags_writes_normalized_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _GateSession(_endpoint())
    fake = _FakeProxmox("old")

    async def fake_gate(_session, endpoint_id, **kwargs):
        del kwargs
        return session.endpoint if endpoint_id == 73 else None

    async def fake_open(_endpoint):
        return fake

    async def fake_get_current_tags(_proxmox, vmid, node, kind):
        del _proxmox, vmid, node, kind
        return ["old"]

    captured: dict[str, object] = {}

    async def fake_set_tags(_proxmox, vmid, node, kind, tags):
        captured["tags"] = tags

    monkeypatch.setattr(proxmox_tags, "_gate", fake_gate)
    monkeypatch.setattr(proxmox_tags, "_open_proxmox_session", fake_open)
    monkeypatch.setattr(proxmox_tags, "_get_current_tags", fake_get_current_tags)
    monkeypatch.setattr(proxmox_tags, "_set_tags", fake_set_tags)

    result = await proxmox_tags.replace_proxmox_tags(
        "qemu",
        100,
        proxmox_tags.ReplaceProxmoxTagsBody(node="pve1", tags=["alpha", "alpha", " beta "]),
        session,  # type: ignore[arg-type]
        endpoint_id=73,
        actor="pytest",
    )

    assert result.tags_after == ["alpha", "beta"]
    assert captured["tags"] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_patch_tags_adds_and_removes(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _GateSession(_endpoint())

    async def fake_gate(_session, endpoint_id, **kwargs):
        del kwargs
        return session.endpoint

    async def fake_open(_endpoint):
        return _FakeProxmox("unused")

    async def fake_get_current_tags(_proxmox, vmid, node, kind):
        del _proxmox, vmid, node, kind
        return ["keep", "drop"]

    captured: dict[str, object] = {}

    async def fake_set_tags(_proxmox, vmid, node, kind, tags):
        captured["tags"] = tags

    monkeypatch.setattr(proxmox_tags, "_gate", fake_gate)
    monkeypatch.setattr(proxmox_tags, "_open_proxmox_session", fake_open)
    monkeypatch.setattr(proxmox_tags, "_get_current_tags", fake_get_current_tags)
    monkeypatch.setattr(proxmox_tags, "_set_tags", fake_set_tags)

    result = await proxmox_tags.patch_proxmox_tags(
        "lxc",
        100,
        proxmox_tags.PatchProxmoxTagsBody(node="pve1", add=["new"], remove=["drop"]),
        session,  # type: ignore[arg-type]
        endpoint_id=73,
        actor="pytest",
    )

    assert result.tags_after == ["keep", "new"]
    assert captured["tags"] == ["keep", "new"]
