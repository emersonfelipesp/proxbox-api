"""Proxmox Backup Server (PBS) integration subpackage.

Read-only PBS-to-NetBox surface installed via ``pip install proxbox-api[pbs]``.

Importing this module is cheap; importing :mod:`proxbox_api.pbs.routes` requires
``proxmox-sdk[pbs]`` (i.e. the ``proxmox_sdk.pbs`` namespace) to be available at
runtime. The app factory mounts ``router`` lazily inside a try/except so the
absence of the PBS extras simply disables ``/pbs/*`` rather than crashing
startup.
"""

from __future__ import annotations

from proxbox_api.pbs.admin import router as admin_router
from proxbox_api.pbs.routes import router

__all__ = ["admin_router", "router"]
