"""Shared types for synchronous reconciliation seams."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from proxbox_api.proxmox_to_netbox.models import ProxmoxVmConfigInput


@dataclass(slots=True)
class PreparedVMState:
    """In-memory VM snapshot prepared from Proxmox + dependency cache."""

    cluster_name: str
    resource: dict[str, object]
    vm_config: dict[str, object]
    vm_config_obj: ProxmoxVmConfigInput
    desired_payload: dict[str, object]
    lookup: dict[str, object]
    now: datetime
    vm_type: str


@dataclass(slots=True)
class NetBoxVMOperation:
    """Queued NetBox VM operation determined by in-memory reconciliation."""

    method: Literal["GET", "CREATE", "UPDATE"]
    prepared: PreparedVMState
    existing_record: dict[str, object] | None = None
    patch_payload: dict[str, object] = field(default_factory=dict)
    # Set by the engine-neutral dispatch policy after it compares the current
    # role with durable sidecar evidence. The caller persists this only after
    # the NetBox operation succeeds.
    role_snapshot_id_to_write: int | None = None
    role_snapshot_previous_id: int | None = None
    role_previous_id: int | None = None
    role_write_applied: bool = False


VMResultKey = tuple[str, int, str]
