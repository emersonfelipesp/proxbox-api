"""Proxmox Datacenter Manager (PDM) integration subpackage.

Read-only PDM-to-NetBox surface installed via ``pip install proxbox-api[pdm]``.

Importing this module is cheap; importing :mod:`proxbox_api.pdm.routes` requires
``proxmox-sdk[pdm]`` (i.e. the ``proxmox_sdk.pdm`` namespace) to be available at
runtime. The app factory mounts ``router`` lazily inside a try/except so the
absence of the PDM extras simply disables ``/pdm/*`` rather than crashing
startup.
"""

from __future__ import annotations

from proxbox_api.pdm.admin import router as admin_router
from proxbox_api.pdm.routes import router

__all__ = ["admin_router", "router"]
