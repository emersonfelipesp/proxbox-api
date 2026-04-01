"""Normalization helpers converting Proxmox raw payloads into NetBox-ready bodies."""

from __future__ import annotations

from datetime import datetime

from proxbox_api.proxmox_to_netbox.errors import ProxmoxToNetBoxError
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxVirtualMachineCreateBody,
    ProxmoxToNetBoxVirtualMachine,
    ProxmoxVmConfigInput,
    ProxmoxVmResourceInput,
)
from proxbox_api.proxmox_to_netbox.netbox_schema import resolve_netbox_schema_contract
from proxbox_api.proxmox_to_netbox.proxmox_schema import proxmox_operation_schema


def _validate_netbox_contract(payload: NetBoxVirtualMachineCreateBody) -> None:
    contract = resolve_netbox_schema_contract()
    source = contract.get("source")
    if source in {"live", "cache"}:
        return
    fallback = contract.get("contract", {})
    required = fallback.get("required_fields", [])
    data = payload.model_dump(exclude_none=True)
    missing = [field for field in required if field not in data]
    if missing:
        raise ProxmoxToNetBoxError(f"NetBox fallback contract required fields missing: {missing}")


def build_virtual_machine_transform(
    resource: ProxmoxVmResourceInput | dict[str, object],
    config: ProxmoxVmConfigInput | dict[str, object] | None,
    *,
    cluster_id: int,
    device_id: int | None,
    role_id: int | None,
    tag_ids: list[int],
    last_updated: datetime | None = None,
) -> NetBoxVirtualMachineCreateBody:
    """Build validated NetBox VM create payload from Proxmox raw payload and config."""

    operation = proxmox_operation_schema(
        path="/cluster/resources",
        method="GET",
    )
    if operation is None:
        raise ProxmoxToNetBoxError(
            "Generated Proxmox OpenAPI is missing /cluster/resources GET operation."
        )

    transform = ProxmoxToNetBoxVirtualMachine(
        resource=resource,
        config=config or {},
        cluster_id=cluster_id,
        device_id=device_id,
        role_id=role_id,
        tag_ids=tag_ids,
        last_updated=last_updated,
    )

    body = transform.as_netbox_create_body()
    _validate_netbox_contract(body)
    return body
