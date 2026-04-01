"""Concrete test data fixtures for e2e tests.

Provides specific test data values that are used across multiple e2e test files.
"""

from __future__ import annotations

import os
import secrets
import string
import time


E2E_TAG_NAME = "proxbox e2e testing"
E2E_TAG_SLUG = "proxbox-e2e-testing"
E2E_TAG_COLOR = "4caf50"
E2E_TAG_DESCRIPTION = "Objects created during proxbox-api e2e testing"

E2E_TAG: dict[str, str] = {
    "name": E2E_TAG_NAME,
    "slug": E2E_TAG_SLUG,
    "color": E2E_TAG_COLOR,
    "description": E2E_TAG_DESCRIPTION,
}

E2E_VM_QEMU: dict[str, object] = {
    "vmid": 99901,
    "name": "e2e-test-qemu",
    "node": "e2e-node-01",
    "status": "running",
    "type": "qemu",
    "maxcpu": 2,
    "maxmem": 4294967296,
    "maxdisk": 53687091200,
}

E2E_VM_QEMU_CONFIG: dict[str, object] = {
    "onboot": 1,
    "agent": 1,
    "unprivileged": 0,
    "searchdomain": "lab.local",
}

E2E_VM_LXC: dict[str, object] = {
    "vmid": 99902,
    "name": "e2e-test-lxc",
    "node": "e2e-node-01",
    "status": "running",
    "type": "lxc",
    "maxcpu": 1,
    "maxmem": 2147483648,
    "maxdisk": 8589934592,
}

E2E_VM_LXC_CONFIG: dict[str, object] = {
    "onboot": 1,
    "unprivileged": 1,
}

E2E_NODE: dict[str, object] = {
    "node": "e2e-node-01",
    "status": "online",
    "uptime": 3600,
}

E2E_CLUSTER: dict[str, object] = {
    "name": "e2e-test-cluster",
    "mode": "pve",
}

E2E_CLUSTER_STATUS: dict[str, object] = {
    "name": "e2e-test-cluster",
    "mode": "pve",
    "node_list": [E2E_NODE],
}

E2E_BACKUP: dict[str, object] = {
    "vmid": 99901,
    "volid": "backup:99901/vm-99901-disk-0.qcow2",
    "storage": "backup",
    "size": 5368709120,
    "ctime": int(time.time()) - 86400,
    "format": "qcow2",
    "subtype": "private",
    "notes": "Test backup",
}

E2E_VM_WITH_INTERFACES: dict[str, object] = {
    "vmid": 99903,
    "name": "e2e-test-with-nics",
    "node": "e2e-node-01",
    "status": "running",
    "type": "qemu",
    "maxcpu": 4,
    "maxmem": 8589934592,
    "maxdisk": 107374182400,
}

E2E_VM_WITH_INTERFACES_CONFIG: dict[str, object] = {
    "onboot": 1,
    "agent": 1,
    "net0": "virtio=aa:bb:cc:dd:ee:01,bridge=vmbr0,firewall=1",
    "net1": "virtio=aa:bb:cc:dd:ee:02,bridge=vmbr1",
}


def generate_unique_resource_prefix() -> str:
    """Generate a unique prefix for test resources.

    Uses timestamp and random suffix to ensure uniqueness across test runs.
    """
    timestamp = int(time.time())
    random_suffix = secrets.token_hex(4)
    return f"e2e_{timestamp}_{random_suffix}"


_e2e_credentials_cache: tuple[str, str] | None = None


def get_e2e_credentials() -> tuple[str, str]:
    """Get or generate e2e test credentials.

    Reads from environment variables or generates new credentials once per process
    so session bootstrap (conftest) and tests share the same username/password.

    Returns:
        Tuple of (username, password).
    """
    global _e2e_credentials_cache
    if _e2e_credentials_cache is not None:
        return _e2e_credentials_cache
    username = os.getenv("PROXBOX_E2E_USERNAME") or f"proxbox_e2e_{secrets.token_hex(4)}"
    password = os.getenv("PROXBOX_E2E_PASSWORD") or _generate_password()
    _e2e_credentials_cache = (username, password)
    return _e2e_credentials_cache


def _generate_password(length: int = 32) -> str:
    """Generate a secure random password."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def get_e2e_demo_config() -> dict[str, object]:
    """Get e2e demo configuration from environment.

    Returns:
        Dict with demo URL, timeout, and headless settings.
    """
    return {
        "demo_url": os.getenv("PROXBOX_E2E_DEMO_URL", "https://demo.netbox.dev"),
        "timeout": float(os.getenv("PROXBOX_E2E_TIMEOUT", "60")),
        "headless": os.getenv("PROXBOX_E2E_HEADLESS", "true").lower() in ("true", "1", "yes"),
    }


def create_test_vm(
    vmid: int,
    name: str | None = None,
    node: str = "e2e-node-01",
    vm_type: str = "qemu",
) -> dict[str, object]:
    """Create a test VM resource dict.

    Args:
        vmid: VM ID.
        name: VM name (defaults to e2e-test-{vmid}).
        node: Node name.
        vm_type: VM type (qemu or lxc).

    Returns:
        VM resource dict for cluster/resources API.
    """
    return {
        "vmid": vmid,
        "name": name or f"e2e-test-{vmid}",
        "node": node,
        "status": "running",
        "type": vm_type,
        "maxcpu": 2,
        "maxmem": 4294967296,
        "maxdisk": 53687091200,
    }


def create_test_node(
    name: str,
    status: str = "online",
    uptime: int = 3600,
) -> dict[str, object]:
    """Create a test node dict.

    Args:
        name: Node name.
        status: Node status.
        uptime: Uptime in seconds.

    Returns:
        Node dict for cluster/status API.
    """
    return {
        "node": name,
        "status": status,
        "uptime": uptime,
    }
