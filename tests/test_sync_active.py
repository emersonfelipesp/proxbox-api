"""Tests for the ``GET /sync/active`` soft-probe endpoint and registry."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from proxbox_api.app.sync_state import (
    _reset_for_tests,
    acquire_active_sync,
    get_active_sync,
    is_active,
    register_active_sync,
    release_active_sync,
)
from proxbox_api.main import app


@pytest.fixture(autouse=True)
def _reset_sync_registry():
    _reset_for_tests()
    yield
    _reset_for_tests()


class TestSyncStateRegistry:
    """Unit tests for the in-memory registry helpers."""

    @pytest.mark.asyncio
    async def test_idle_state(self):
        snapshot = await get_active_sync()
        assert snapshot["active"] is False
        assert snapshot["started_at"] is None
        assert snapshot["id"] is None
        assert snapshot["runs"] == []
        assert await is_active() is False

    @pytest.mark.asyncio
    async def test_acquire_release_round_trip(self):
        entry = await acquire_active_sync("op-1", kind="full-update")
        try:
            snapshot = await get_active_sync()
            assert snapshot["active"] is True
            assert snapshot["id"] == "op-1"
            assert snapshot["kind"] == "full-update"
            assert snapshot["started_at"] is not None
            assert len(snapshot["runs"]) == 1
            assert await is_active() is True
        finally:
            await release_active_sync(entry)
        snapshot = await get_active_sync()
        assert snapshot["active"] is False
        assert snapshot["runs"] == []

    @pytest.mark.asyncio
    async def test_context_manager_cleans_on_exception(self):
        with pytest.raises(RuntimeError):
            async with register_active_sync("op-boom", kind="full-update"):
                assert await is_active() is True
                raise RuntimeError("boom")
        assert await is_active() is False

    @pytest.mark.asyncio
    async def test_context_manager_cleans_on_cancellation(self):
        started = asyncio.Event()

        async def worker():
            async with register_active_sync("op-cancel", kind="full-update"):
                started.set()
                await asyncio.sleep(60)

        task = asyncio.create_task(worker())
        await started.wait()
        assert await is_active() is True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await is_active() is False

    @pytest.mark.asyncio
    async def test_reports_oldest_run_first_with_concurrent_runs(self):
        async with register_active_sync("op-first", kind="full-update"):
            async with register_active_sync("op-second", kind="full-update"):
                snapshot = await get_active_sync()
                assert snapshot["active"] is True
                assert snapshot["id"] == "op-first"
                assert {r["id"] for r in snapshot["runs"]} == {
                    "op-first",
                    "op-second",
                }
        assert await is_active() is False


class TestSyncActiveEndpoint:
    """HTTP-level tests for the ``GET /sync/active`` probe."""

    @pytest.mark.asyncio
    async def test_idle_returns_inactive(self, authenticated_client):
        resp = await authenticated_client.get("/sync/active")
        assert resp.status_code == 200
        body = resp.json()
        assert body["active"] is False
        assert body["started_at"] is None
        assert body["id"] is None
        assert body["runs"] == []

    @pytest.mark.asyncio
    async def test_reports_in_flight_run(self, authenticated_client):
        async with register_active_sync("op-live", kind="full-update"):
            resp = await authenticated_client.get("/sync/active")
            assert resp.status_code == 200
            body = resp.json()
            assert body["active"] is True
            assert body["id"] == "op-live"
            assert body["kind"] == "full-update"
            assert body["started_at"] is not None
            assert len(body["runs"]) == 1
            assert body["runs"][0]["id"] == "op-live"
        resp = await authenticated_client.get("/sync/active")
        assert resp.json()["active"] is False

    @pytest.mark.asyncio
    async def test_requires_auth(self, client_with_fake_netbox):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/sync/active")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_response_shape_matches_schema(self, authenticated_client):
        resp = await authenticated_client.get("/sync/active")
        assert resp.status_code == 200
        body = resp.json()
        expected_keys = {"active", "started_at", "id", "kind", "runs"}
        assert expected_keys.issubset(body.keys())
