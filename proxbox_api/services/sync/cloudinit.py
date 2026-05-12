"""Write the Proxmox cloud-init reflection row to NetBox (issue #363).

The plugin endpoint lives at ``/api/plugins/proxbox/vm-cloudinit/`` and is
populated by proxbox-api after each VM upsert. ``overwrite_vm_cloudinit``
on :class:`SyncOverwriteFlags` gates patching of an existing row; first-time
creates are always allowed so a sync from a fresh install isn't silently
empty.
"""

from __future__ import annotations

from typing import Any

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxCloudInitSyncState,
    ProxmoxVmConfigInput,
)
from proxbox_api.schemas.sync import SyncOverwriteFlags

CLOUDINIT_ENDPOINT = "/api/plugins/proxbox/vm-cloudinit/"
CLOUDINIT_PATCHABLE_FIELDS = frozenset({"ciuser", "sshkeys", "ipconfig0", "sshkeys_truncated"})


async def sync_vm_cloudinit(
    netbox_session: object,
    *,
    vm_id: int,
    proxmox_config: ProxmoxVmConfigInput | dict[str, Any] | None,
    overwrite_flags: SyncOverwriteFlags | None = None,
) -> dict[str, Any] | None:
    """Upsert the ``ProxmoxVMCloudInit`` row that reflects this VM's cloud-init keys.

    Returns the NetBox record dict on write, or ``None`` when there's nothing
    cloud-init-shaped to mirror.
    """
    if proxmox_config is None:
        return None

    if isinstance(proxmox_config, dict):
        try:
            config = ProxmoxVmConfigInput.model_validate(proxmox_config)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("cloud-init: config validation failed for vm_id=%s: %s", vm_id, exc)
            return None
    else:
        config = proxmox_config

    payload = config.cloudinit_payload()
    if payload is None:
        return None
    payload["virtual_machine"] = vm_id

    patchable = (
        CLOUDINIT_PATCHABLE_FIELDS
        if overwrite_flags is None or overwrite_flags.overwrite_vm_cloudinit
        else frozenset()
    )

    record = await rest_reconcile_async(
        netbox_session,
        CLOUDINIT_ENDPOINT,
        lookup={"virtual_machine_id": vm_id},
        payload=payload,
        schema=NetBoxCloudInitSyncState,
        patchable_fields=patchable,
        current_normalizer=lambda existing: {
            "virtual_machine": existing.get("virtual_machine"),
            "ciuser": existing.get("ciuser") or "",
            "sshkeys": existing.get("sshkeys") or "",
            "ipconfig0": existing.get("ipconfig0") or "",
            "sshkeys_truncated": bool(existing.get("sshkeys_truncated")),
        },
    )
    return record.serialize() if hasattr(record, "serialize") else None
