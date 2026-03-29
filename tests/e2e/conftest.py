"""Pytest configuration and fixtures for e2e tests.

This module provides session-scoped fixtures for e2e testing with NetBox demo.
It handles:
- Demo profile bootstrap (Playwright authentication)
- NetBox session creation
- E2E tag management
- Mock Proxmox data

Usage:
    pytest tests/e2e/ -v
    pytest tests/e2e/ -v -n auto  # with pytest-xdist

Environment variables:
    PROXBOX_E2E_USERNAME: Demo username (default: auto-generated)
    PROXBOX_E2E_PASSWORD: Demo password (default: auto-generated)
    PROXBOX_E2E_DEMO_URL: NetBox demo URL (default: https://demo.netbox.dev)
    PROXBOX_E2E_TIMEOUT: Browser timeout in seconds (default: 60)
    PROXBOX_E2E_HEADLESS: Run browser headless (default: true)
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, AsyncGenerator

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from netbox_sdk.config import Config
    from netbox_sdk.facade import Api

from proxbox_api.e2e.demo_auth import (
    PlaywrightNotInstalledError,
    bootstrap_demo_profile,
)
from proxbox_api.e2e.fixtures.proxmox_mock import (
    MockProxmoxCluster,
    create_minimal_cluster,
    create_multi_cluster,
)
from proxbox_api.e2e.fixtures.test_data import (
    E2E_TAG,
    generate_unique_resource_prefix,
    get_e2e_credentials,
    get_e2e_demo_config,
)
from proxbox_api.e2e.session import (
    build_e2e_tag_refs,
    create_netbox_demo_session,
    ensure_e2e_tag,
)


@pytest.fixture(scope="session")
def event_loop_policy():
    """Use the default event loop policy for the session."""
    return asyncio.DefaultEventLoopPolicy()


@pytest_asyncio.fixture(scope="session")
async def netbox_demo_config() -> "Config":
    """Bootstrap demo profile once per test session.

    This fixture creates a NetBox demo user and provisions an API token.
    It is session-scoped to avoid repeated browser launches.

    Returns:
        Config object with demo credentials and token.

    Raises:
        PlaywrightNotInstalledError: If Playwright is not installed.
    """
    username, password = get_e2e_credentials()
    config = get_e2e_demo_config()

    print(f"\n[E2E Setup] Bootstrapping demo profile for user: {username}")

    try:
        cfg = await bootstrap_demo_profile(
            username=username,
            password=password,
            timeout=config["timeout"],
            headless=config["headless"],
            token_name="proxbox-e2e",
        )
        print("[E2E Setup] Demo profile bootstrapped successfully")
        print(f"[E2E Setup] Token version: {cfg.token_version}")
        return cfg
    except PlaywrightNotInstalledError:
        pytest.skip(
            "Playwright not installed. Run: pip install playwright && playwright install chromium"
        )


@pytest_asyncio.fixture(scope="session")
async def netbox_demo_session(netbox_demo_config: "Config") -> "Api":
    """Create NetBox API session from demo config.

    This fixture depends on netbox_demo_config to ensure the demo profile
    exists before creating the session.

    Returns:
        Async NetBox API instance.
    """
    print("[E2E Setup] Creating NetBox session...")
    api = await create_netbox_demo_session(netbox_demo_config)
    print("[E2E Setup] NetBox session created")
    return api


@pytest_asyncio.fixture(scope="session")
async def e2e_tag(netbox_demo_session: "Api") -> dict[str, Any]:
    """Ensure the 'proxbox e2e testing' tag exists.

    Creates the tag if it doesn't exist. This is session-scoped
    since the tag should be shared across all tests.

    Returns:
        Tag dict with id, name, slug, color, url.
    """
    print("[E2E Setup] Ensuring e2e tag exists...")
    tag = await ensure_e2e_tag(netbox_demo_session)
    print(f"[E2E Setup] E2E tag ready: {tag['name']} (id={tag['id']})")
    return tag


@pytest_asyncio.fixture(scope="session")
async def e2e_tag_refs(e2e_tag: dict[str, Any]) -> list[dict[str, Any]]:
    """Get tag refs list for NetBox API payloads.

    Returns:
        List containing the e2e tag ref dict.
    """
    return build_e2e_tag_refs(e2e_tag)


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
    netbox_demo_session: "Api", e2e_tag: dict[str, Any]
) -> AsyncGenerator[None, None]:
    """Fixture that optionally cleans up test objects after test.

    Note: This is disabled by default since NetBox demo resets daily.
    Uncomment the cleanup code below to enable cleanup between tests.

    Args:
        netbox_demo_session: NetBox API session.
        e2e_tag: E2E testing tag.

    Yields:
        Control to the test.
    """
    yield
    # Cleanup is disabled since NetBox demo resets daily
    # To enable cleanup, uncomment below:
    # from proxbox_api.e2e.session import cleanup_e2e_objects
    # deleted = await cleanup_e2e_objects(netbox_demo_session)
    # print(f"[E2E Cleanup] Deleted objects: {deleted}")


@pytest.fixture
def skip_if_no_playwright():
    """Skip test if Playwright is not installed."""
    import importlib.util

    if importlib.util.find_spec("playwright") is None:
        pytest.skip("Playwright not installed")


@pytest.fixture
def mock_proxmox_session(minimal_cluster: MockProxmoxCluster):
    """Create a mock Proxmox session that returns fixture data.

    This fixture monkeypatches the Proxmox session to return
    the mock cluster data instead of making real API calls.

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
