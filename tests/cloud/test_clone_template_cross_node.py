"""Cross-node cloud clones must resolve the template's own node (issue #616).

The fake session enforces the Proxmox request schema for ``cluster/resources``:
``type`` accepts only ``vm``, ``storage``, ``node``, and ``sdn``. Before the fix
the resolver sent ``type=qemu``, the lookup failed, and the clone fell back to
reading the template on the destination node, so a template on shared storage
could never be cloned onto another node.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from proxbox_api.routes.cloud import provision as provision_route
from proxbox_api.routes.intent.cloud_init import CloudInitPayload
from proxbox_api.schemas.cloud_provision import CloudVMProvisionRequest

_ALLOWED_RESOURCE_TYPES = {"vm", "storage", "node", "sdn"}


class _ProxmoxParameterError(Exception):
    """Stands in for Proxmox's HTTP 400 parameter verification failure."""


class _Endpoint:
    def __init__(self, session: _FakeProxmoxApi, path: tuple[str, ...]) -> None:
        self._session = session
        self._path = path

    def __getattr__(self, name: str) -> _Endpoint:
        return _Endpoint(self._session, (*self._path, name))

    def __call__(self, segment: object) -> _Endpoint:
        return _Endpoint(self._session, (*self._path, str(segment)))

    def get(self, **params: Any) -> object:
        return self._session.handle("GET", self._path, params)

    def post(self, **params: Any) -> object:
        return self._session.handle("POST", self._path, params)


class _FakeProxmoxApi:
    """Two-node cluster with template 9243 owned by node01."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...], dict[str, Any]]] = []

    def __call__(self, path: str) -> _Endpoint:
        return _Endpoint(self, tuple(path.split("/")))

    def __getattr__(self, name: str) -> _Endpoint:
        return _Endpoint(self, (name,))

    def handle(self, method: str, path: tuple[str, ...], params: dict[str, Any]) -> object:
        self.calls.append((method, path, params))
        if path == ("cluster", "resources"):
            if "type" in params and params["type"] not in _ALLOWED_RESOURCE_TYPES:
                raise _ProxmoxParameterError(f"type: value {params['type']!r} is not allowed")
            return [
                {"id": "qemu/9243", "type": "qemu", "vmid": 9243, "node": "node01"},
                {"id": "lxc/9243", "type": "lxc", "vmid": 9243, "node": "node05"},
                {"id": "node/node04", "type": "node", "node": "node04"},
            ]
        if path == ("nodes", "node01", "qemu", "9243", "clone") and method == "POST":
            return "UPID:node01:00000001:00000001:00000001:qmclone:9243:root@pam:"
        if path[:1] == ("nodes",) and path[-1] == "config" and method == "GET":
            if path[1:4] == ("node01", "qemu", "9243"):
                return {"name": "pbs-4-2-debian-13-cluster01", "template": 1}
            raise _ProxmoxParameterError(f"configuration for {'/'.join(path)} does not exist")
        raise AssertionError(f"unexpected Proxmox call: {method} {'/'.join(path)} {params}")


class _FakeProxmoxSession:
    def __init__(self) -> None:
        self.session = _FakeProxmoxApi()


def _request(target_node: str) -> CloudVMProvisionRequest:
    return CloudVMProvisionRequest(
        endpoint_id=14,
        template_vmid=9243,
        new_vmid=10160,
        new_name="PBS06",
        target_node=target_node,
        storage="local-zfs-hdd",
        cloud_init=CloudInitPayload(user="root"),
    )


@pytest.fixture(autouse=True)
def _no_task_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision_route, "_should_wait_for_upid", lambda: False)


@pytest.mark.parametrize("target_node", ["node04", "node01"])
async def test_clone_posts_on_template_node_with_destination_target(target_node: str) -> None:
    proxmox = _FakeProxmoxSession()

    upid = await provision_route._clone_template_vm(proxmox, _request(target_node))

    assert upid is not None and upid.startswith("UPID:node01:")
    resource_queries = [
        params for method, path, params in proxmox.session.calls if path == ("cluster", "resources")
    ]
    assert resource_queries == [{"type": "vm"}]
    clones = [
        (path, params)
        for method, path, params in proxmox.session.calls
        if method == "POST" and path[-1] == "clone"
    ]
    assert clones == [
        (
            ("nodes", "node01", "qemu", "9243", "clone"),
            {
                "newid": 10160,
                "name": "PBS06",
                "target": target_node,
                "full": 1,
                "storage": "local-zfs-hdd",
            },
        )
    ]


async def test_clone_reports_missing_template_without_cloning() -> None:
    proxmox = _FakeProxmoxSession()
    request = _request("node04").model_copy(update={"template_vmid": 9999})

    with pytest.raises(HTTPException) as exc_info:
        await provision_route._clone_template_vm(proxmox, request)

    assert exc_info.value.status_code == 404
    assert not [call for call in proxmox.session.calls if call[0] == "POST"]
