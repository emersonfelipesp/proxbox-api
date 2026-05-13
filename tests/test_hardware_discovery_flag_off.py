"""When ``hardware_discovery_enabled`` is False, no SSH socket may open.

Asserts the orchestrator's kill switch: with the flag off the deferred imports
of ``proxmox_sdk.ssh`` and ``proxmox_sdk.node.hardware`` must never resolve and
``RemoteSSHClient`` must never be instantiated.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from proxbox_api.services import hardware_discovery


@pytest.mark.asyncio
async def test_run_for_nodes_no_op_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardware_discovery, "is_enabled", lambda: False)

    sentinel = MagicMock(side_effect=AssertionError("RemoteSSHClient must not be constructed"))
    fake_ssh = types.ModuleType("proxmox_sdk.ssh")
    fake_ssh.RemoteSSHClient = sentinel  # type: ignore[attr-defined]
    fake_ssh.HostKeyMismatch = type("HostKeyMismatch", (Exception,), {})  # type: ignore[attr-defined]
    fake_ssh.SshTimeout = type("SshTimeout", (Exception,), {})  # type: ignore[attr-defined]
    fake_ssh.SshAuthFailed = type("SshAuthFailed", (Exception,), {})  # type: ignore[attr-defined]
    fake_ssh.CommandNotAllowed = type("CommandNotAllowed", (Exception,), {})  # type: ignore[attr-defined]
    fake_ssh.OutputTooLarge = type("OutputTooLarge", (Exception,), {})  # type: ignore[attr-defined]
    fake_hw = types.ModuleType("proxmox_sdk.node.hardware")

    async def _explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("discover_node must not be called")

    fake_hw.discover_node = _explode  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "proxmox_sdk.ssh", fake_ssh)
    monkeypatch.setitem(sys.modules, "proxmox_sdk.node.hardware", fake_hw)

    await hardware_discovery.run_for_nodes(
        netbox_session=MagicMock(),
        nodes=[{"id": 1, "name": "node-a", "host": "10.0.0.1"}],
        bridge=None,
    )
    sentinel.assert_not_called()
