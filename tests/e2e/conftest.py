"""Pytest configuration and fixtures for e2e tests.

This module provides session-scoped fixtures for e2e testing with a local NetBox instance.
It handles:
- NetBox session creation from environment variables
- E2E tag management
- Mock Proxmox data

Usage:
    pytest tests/e2e/ -v

Environment variables:
    PROXBOX_E2E_NETBOX_URL: NetBox base URL (required, e.g., http://127.0.0.1:18080)
    PROXBOX_E2E_NETBOX_TOKEN: NetBox API token (required)
    PROXMOX_API_MODE: Set to "mock" for mock Proxmox (default)
    PROXMOX_MOCK_PUBLISHED_URL: Proxmox mock HTTP URL (optional)
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, AsyncGenerator

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from netbox_sdk.facade import Api

from proxbox_api.e2e.fixtures.proxmox_sdk_mock import (
    MockProxmoxCluster,
    create_minimal_cluster,
    create_multi_cluster,
)
from proxbox_api.e2e.fixtures.test_data import E2E_TAG, generate_unique_resource_prefix
from proxbox_api.e2e.session import (
    build_e2e_tag_refs,
    create_netbox_e2e_session,
)
from proxbox_api.netbox_rest import _reset_netbox_globals


@pytest.fixture(scope="session", autouse=True)
def reset_netbox_globals_session():
    """Reset netbox_rest module globals before and after the E2E session."""
    _reset_netbox_globals()
    yield
    _reset_netbox_globals()


@pytest.fixture(scope="session")
def event_loop_policy():
    """Use the default event loop policy for the session."""
    import asyncio

    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="session")
def netbox_e2e_config() -> dict[str, str]:
    """Get NetBox E2E configuration from environment variables.

    Returns:
        Dict with base_url and token.

    Raises:
        pytest.Skip: If required environment variables are not set.
    """
    url = os.environ.get("PROXBOX_E2E_NETBOX_URL", "").strip()
    token = os.environ.get("PROXBOX_E2E_NETBOX_TOKEN", "").strip()

    if not url or not token:
        pytest.skip(
            "PROXBOX_E2E_NETBOX_URL and PROXBOX_E2E_NETBOX_TOKEN environment variables required"
        )

    return {"base_url": url, "token": token}


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def netbox_e2e_session(netbox_e2e_config: dict[str, str]) -> AsyncGenerator["Api", None]:
    """Create NetBox API session from environment config.

    This fixture creates a NetBox API session using URL and token from
    environment variables. It is session-scoped to avoid repeated connections.

    Returns:
        Async NetBox API instance.
    """
    print(f"\n[E2E Setup] Connecting to NetBox at: {netbox_e2e_config['base_url']}")
    api = await create_netbox_e2e_session(
        base_url=netbox_e2e_config["base_url"],
        token=netbox_e2e_config["token"],
    )
    print("[E2E Setup] NetBox session created")
    try:
        yield api
    finally:
        await api.client.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def e2e_tag(netbox_e2e_session: "Api") -> dict[str, Any]:
    """Ensure the 'proxbox e2e testing' tag exists.

    Creates the tag if it doesn't exist. This is session-scoped
    since the tag should be shared across all tests.

    Returns:
        Tag dict with id, name, slug, color, url.
    """
    from proxbox_api.netbox_rest import ensure_tag_async

    print("[E2E Setup] Ensuring e2e tag exists...")
    tag = await ensure_tag_async(
        netbox_e2e_session,
        name=E2E_TAG["name"],
        slug=E2E_TAG["slug"],
        color=E2E_TAG["color"],
        description=E2E_TAG["description"],
    )

    if isinstance(tag, dict):
        tag_data = tag
    elif hasattr(tag, "serialize"):
        tag_data = tag.serialize()
    else:
        tag_data = {
            "id": getattr(tag, "id", None),
            "name": getattr(tag, "name", None),
            "slug": getattr(tag, "slug", None),
            "color": getattr(tag, "color", None),
            "url": getattr(tag, "url", None),
        }

    print(f"[E2E Setup] E2E tag ready: {tag_data.get('name')} (id={tag_data.get('id')})")
    return tag_data


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def e2e_tag_refs(e2e_tag: dict[str, Any]) -> list[dict[str, Any]]:
    """Get tag refs list for NetBox API payloads.

    Returns:
        List containing the e2e tag ref dict.
    """
    return build_e2e_tag_refs(e2e_tag)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def e2e_shared_proxmox_site(netbox_e2e_session: "Api", e2e_tag: dict[str, Any]) -> Any:
    """Single DCIM site reused by VM e2e tests."""

    from proxbox_api.netbox_rest import nested_tag_payload, rest_reconcile_async
    from proxbox_api.proxmox_to_netbox.models import NetBoxSiteSyncState

    tag_refs = nested_tag_payload(e2e_tag)
    return await rest_reconcile_async(
        netbox_e2e_session,
        "/api/dcim/sites/",
        lookup={"slug": "proxbox-api-e2e-shared-site"},
        payload={
            "name": "Proxbox API E2E Shared Site",
            "slug": "proxbox-api-e2e-shared-site",
            "status": "active",
            "tags": tag_refs,
        },
        schema=NetBoxSiteSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "status": record.get("status"),
            "tags": record.get("tags"),
        },
    )


@pytest.fixture
def unique_prefix() -> str:
    """Generate unique prefix for test resources in this test.

    Returns:
        Unique string prefix for resource naming.
    """
    return generate_unique_resource_prefix()


@pytest.fixture
def minimal_cluster(unique_prefix: str) -> MockProxmoxCluster:
    """Create a minimal cluster for testing.

    Args:
        unique_prefix: Prefix for resource naming.

    Returns:
        MockProxmoxCluster with single node and 2 VMs.
    """
    return create_minimal_cluster(prefix=unique_prefix)


@pytest.fixture
def multi_cluster(unique_prefix: str) -> list[MockProxmoxCluster]:
    """Create multiple clusters for testing.

    Args:
        unique_prefix: Prefix for resource naming.

    Returns:
        List of 2 MockProxmoxCluster instances.
    """
    return create_multi_cluster(prefix=unique_prefix)


@pytest.fixture
def e2e_tag_info() -> dict[str, str]:
    """Get e2e tag configuration.

    Returns:
        Dict with tag name, slug, color.
    """
    return E2E_TAG


@pytest_asyncio.fixture
async def clean_test_objects(
    netbox_e2e_session: "Api", e2e_tag: dict[str, Any]
) -> AsyncGenerator[None, None]:
    """Fixture that optionally cleans up test objects after test.

    Cleanup is disabled - relying on fresh NetBox instance per run.

    Args:
        netbox_e2e_session: NetBox API session.
        e2e_tag: E2E testing tag.

    Yields:
        Control to the test.
    """
    yield


@pytest.fixture
def mock_proxmox_session(minimal_cluster: MockProxmoxCluster):
    """Create a mock Proxmox session that returns fixture data.

    Args:
        minimal_cluster: The mock cluster data.

    Returns:
        Dict with cluster and resource data.
    """
    return {
        "cluster": minimal_cluster.to_cluster_status(),
        "resources": minimal_cluster.to_cluster_resources(),
        "storage": minimal_cluster.storage,
    }


@pytest.fixture(params=["backend", "http_published", "http_local"])
async def proxmox_mock_client(
    request, proxmox_mock_backend, proxmox_mock_http_published, proxmox_mock_http_local
):
    """Parametrized fixture that provides Proxmox SDK in all mock modes.

    This fixture runs tests against all three mock backends:
    - backend: In-process MockBackend (fastest)
    - http_published: HTTP container (published image, port 8006)
    - http_local: HTTP container (local build, port 8007)

    Usage:
        @pytest.mark.mock_backend
        @pytest.mark.mock_http
        async def test_something(proxmox_mock_client):
            # Test runs 3 times: once per mock backend
            vms = await proxmox_mock_client.nodes.get()
    """
    if request.param == "backend":
        yield proxmox_mock_backend
    elif request.param == "http_published":
        yield proxmox_mock_http_published
    else:
        yield proxmox_mock_http_local
