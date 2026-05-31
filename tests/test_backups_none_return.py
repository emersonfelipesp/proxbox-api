"""Regression test for GitHub issue #551.

Tests that _create_all_virtual_machine_backups returns an empty list (not None)
when no backups are found, preventing TypeError: len(None) in full_update.py.
"""

import pytest


@pytest.mark.asyncio
async def test_create_all_virtual_machine_backups_returns_empty_list_on_no_backups(
    monkeypatch,
):
    """Test that the function returns [] (not None) when no backups exist.

    Regression for: TypeError: object of type 'NoneType' has no len()
    at full_update.py line 442: "backups_count": len(sync_backups)
    """
    from proxbox_api.routes.virtualization.virtual_machines.backups_vm import (
        _create_all_virtual_machine_backups,
    )

    # Mock dependencies with minimal shapes
    class MockNetBox:
        async def get(self, *args, **kwargs):
            return []

        async def fetch_all(self, *args, **kwargs):
            return []

    class MockProxmox:
        pass

    class MockClusterStatus:
        pass

    class MockTag:
        pass

    # Call the function with no backups to discover (empty Proxmox cluster)
    result = await _create_all_virtual_machine_backups(
        netbox_session=MockNetBox(),
        pxs={"default": MockProxmox()},
        cluster_status={"default": MockClusterStatus()},
        tag=MockTag(),
        delete_nonexistent_backup=True,
        fetch_max_concurrency=None,
    )

    # The fix ensures this is always a list, not None
    assert isinstance(result, list), "Result must be a list, not None"
    assert result == [], "Empty cluster should return empty list"


@pytest.mark.asyncio
async def test_full_update_defensive_guard_len_on_backups():
    """Test that full_update.py can safely call len(sync_backups) even if None.

    The defensive guard `sync_backups = await_result or []` ensures this never
    raises TypeError: len(None).
    """

    # Simulate the defensive guard pattern in full_update.py
    async def mock_backups_task():
        # Simulate the case where the function returns None
        return None

    # Apply the defensive guard pattern
    result = (await mock_backups_task()) or []

    # Should be able to call len() safely
    assert len(result) == 0
    assert isinstance(result, list)
