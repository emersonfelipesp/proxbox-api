"""Mock Proxmox API utilities for testing using proxmox-sdk SDK."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from proxbox_api.exception import ProxboxException


class MockProxmoxContext:
    """Context manager for creating mock Proxmox sessions in tests.

    Automatically detects the best mock backend based on environment:
    - In-process MockBackend (fastest)
    - HTTP container (published image)
    - HTTP container (local build)

    Usage:
        async with MockProxmoxContext() as proxmox:
            vms = await proxmox.nodes("pve01").qemu.get()
    """

    def __init__(
        self,
        backend: str | None = None,
        host: str | None = None,
        verify_ssl: bool = False,
    ) -> None:
        """Initialize mock context.

        Args:
            backend: Override backend selection. Options:
                     - "mock": in-process MockBackend
                     - "http-published": HTTP container (published image, port 8006)
                     - "http-local": HTTP container (local build, port 8007)
                     - None: auto-detect based on environment
            host: Override host URL for HTTP backends
            verify_ssl: Whether to verify SSL certificates (default: False for mock)
        """
        self._backend = backend
        self._host = host
        self._verify_ssl = verify_ssl
        self._sdk = None
        self._auto_close = True

    def _detect_backend(self) -> tuple[str, str]:
        """Detect the appropriate backend and host.

        Returns:
            Tuple of (backend_type, host_url)
        """
        if self._backend:
            backend = self._backend
        elif os.getenv("PROXMOX_API_MODE") == "mock":
            backend = "mock"
        elif os.getenv("PYTEST_CURRENT_TEST"):
            backend = "mock"
        else:
            backend = "mock"

        if backend == "mock":
            return "mock", "mock"

        if backend == "http-published":
            host = self._host or os.getenv("PROXMOX_MOCK_PUBLISHED_URL", "http://localhost:8006")
            return "https", host

        if backend == "http-local":
            host = self._host or os.getenv("PROXMOX_MOCK_LOCAL_URL", "http://localhost:8007")
            return "https", host

        return backend, self._host or "http://localhost:8006"

    async def __aenter__(self) -> Any:
        """Create and return mock SDK session."""
        from proxmox_sdk import ProxmoxSDK

        backend, host = self._detect_backend()

        self._sdk = ProxmoxSDK(
            host=host,
            backend=backend,
            verify_ssl=self._verify_ssl,
        )

        return self._sdk

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Close SDK session if auto_close is enabled."""
        if self._auto_close and self._sdk:
            try:
                await self._sdk.close()
            except Exception:
                pass

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Allow context to be called as a session."""
        if self._sdk is None:
            raise ProxboxException(
                message="MockProxmoxContext not entered",
                detail="Use 'async with MockProxmoxContext() as proxmox:'",
            )
        return self._sdk(*args, **kwargs)


@asynccontextmanager
async def mock_proxmox_session(
    backend: str | None = None,
    host: str | None = None,
):
    """Async context manager for creating a mock Proxmox session.

    This is a convenience wrapper around MockProxmoxContext.

    Args:
        backend: Backend type override
        host: Host URL override

    Yields:
        ProxmoxSDK instance in mock mode
    """
    async with MockProxmoxContext(backend=backend, host=host) as sdk:
        yield sdk


def reset_mock_state() -> None:
    """Reset the shared mock state between tests.

    This clears all data stored in the MockBackend's shared store.
    Call this in test teardown to ensure test isolation.
    """
    try:
        from proxmox_sdk.mock.schema_helpers import schema_fingerprint
        from proxmox_sdk.mock.state import shared_mock_store
        from proxmox_sdk.schema import load_proxmox_generated_openapi

        doc = load_proxmox_generated_openapi()
        if doc:
            fp = schema_fingerprint(doc)
            store = shared_mock_store(fp)
            store.reset()
    except Exception:
        pass


async def seed_minimal_cluster(sdk: Any, prefix: str = "test") -> dict[str, Any]:
    """Seed a minimal Proxmox cluster into the mock store.

    Creates:
    - 1 node (pve01)
    - 2 VMs (one QEMU, one LXC)

    Args:
        sdk: ProxmoxSDK instance (mock or real)
        prefix: Prefix for resource names

    Returns:
        Dict with cluster metadata
    """
    cluster_name = f"{prefix}-cluster"

    try:
        await sdk.nodes.post(
            node="pve01",
            type="node",
            status="online",
        )

        await sdk.cluster.config.post(
            name=cluster_name,
        )

        vmid_qemu = int(f"{prefix}100")
        await sdk.nodes("pve01").qemu.post(
            vmid=vmid_qemu,
            name=f"{prefix}-vm-1",
            ostype="l26",
            net0="virtio=AA:BB:CC:DD:EE:F1,bridge=vmbr0",
        )

        vmid_lxc = int(f"{prefix}200")
        await sdk.nodes("pve01").lxc.post(
            vmid=vmid_lxc,
            name=f"{prefix}-container-1",
            ostype="debian",
        )

        return {
            "name": cluster_name,
            "node": "pve01",
            "vmid_qemu": vmid_qemu,
            "vmid_lxc": vmid_lxc,
        }
    except Exception as error:
        raise ProxboxException(
            message="Failed to seed minimal cluster",
            python_exception=str(error),
        )


async def seed_multi_cluster(sdk: Any, prefix: str = "test") -> dict[str, Any]:
    """Seed a multi-node Proxmox cluster into the mock store.

    Creates:
    - 3 nodes (pve01, pve02, pve03)
    - 4 VMs across nodes
    - 2 storage definitions

    Args:
        sdk: ProxmoxSDK instance (mock or real)
        prefix: Prefix for resource names

    Returns:
        Dict with cluster metadata
    """
    cluster_name = f"{prefix}-cluster"

    try:
        for node_name in ["pve01", "pve02", "pve03"]:
            await sdk.nodes.post(
                node=node_name,
                type="node",
                status="online",
            )

        await sdk.cluster.config.post(
            name=cluster_name,
        )

        vms = []
        for i, node_name in enumerate(["pve01", "pve01", "pve02", "pve03"]):
            vmid = int(f"{prefix}{100 + i}")
            await sdk.nodes(node_name).qemu.post(
                vmid=vmid,
                name=f"{prefix}-vm-{i + 1}",
                ostype="l26",
            )
            vms.append({"vmid": vmid, "node": node_name})

        await sdk.cluster.storage.post(
            storage="local",
            type="dir",
            content="rootdir,images,backup",
            shared=False,
        )

        await sdk.cluster.storage.post(
            storage="shared",
            type="dir",
            content="images,rootdir",
            shared=True,
            nodes="all",
        )

        return {
            "name": cluster_name,
            "nodes": ["pve01", "pve02", "pve03"],
            "vms": vms,
        }
    except Exception as error:
        raise ProxboxException(
            message="Failed to seed multi cluster",
            python_exception=str(error),
        )
