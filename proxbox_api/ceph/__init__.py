"""Ceph integration subpackage.

Ceph v1 is a read-only Proxmox VE-backed surface.  The routes mounted here use
the existing Proxmox endpoint/session dependency and wrap each PVE session in
``proxmox_sdk.ceph.CephClient`` when the SDK namespace is installed, falling
back to proxbox-api's internal PVE path facade while that SDK surface is
unreleased.
"""

from __future__ import annotations

from proxbox_api.ceph.routes import router

__all__ = ["router"]
