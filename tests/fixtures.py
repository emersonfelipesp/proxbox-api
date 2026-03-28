from __future__ import annotations

from typing import Any

PROXMOX_VM_RESOURCE: dict[str, Any] = {
    "vmid": 101,
    "name": "db-vm-01",
    "node": "pve01",
    "status": "running",
    "type": "qemu",
    "maxcpu": 4,
    "maxmem": 8_589_934_592,
    "maxdisk": 107_374_182_400,
}

PROXMOX_VM_CONFIG: dict[str, Any] = {
    "onboot": 1,
    "agent": 1,
    "unprivileged": 0,
    "searchdomain": "lab.local",
}

NETBOX_OPENAPI_SNAPSHOT: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "NetBox API", "version": "4.2"},
    "paths": {
        "/api/virtualization/virtual-machines/": {
            "get": {},
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name", "status", "cluster"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "status": {"type": "string"},
                                    "cluster": {"type": "integer"},
                                },
                            }
                        }
                    }
                }
            },
        }
    },
}
