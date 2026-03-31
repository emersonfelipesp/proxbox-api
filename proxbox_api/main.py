"""FastAPI application entrypoint and route registration."""

from __future__ import annotations

# Re-exports for tests and callers that patch ``proxbox_api.main.*``.
from fastapi.responses import StreamingResponse

from proxbox_api.app.factory import create_app
from proxbox_api.app.full_update import full_update_sync, full_update_sync_stream
from proxbox_api.app.netbox_session import get_raw_netbox_session
from proxbox_api.app.root_meta import standalone_info
from proxbox_api.routes.proxmox.runtime_generated import register_generated_proxmox_routes
from proxbox_api.routes.virtualization.virtual_machines import create_virtual_machines
from proxbox_api.services.sync.devices import create_proxmox_devices

app = create_app()

__all__ = (
    "StreamingResponse",
    "app",
    "create_proxmox_devices",
    "create_virtual_machines",
    "full_update_sync",
    "full_update_sync_stream",
    "get_raw_netbox_session",
    "register_generated_proxmox_routes",
    "standalone_info",
)
