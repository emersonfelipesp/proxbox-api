from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from proxbox_api.log_buffer import LogLevel
from proxbox_api.main import app
from proxbox_api.routes.admin import logs as admin_logs


@pytest.mark.asyncio
async def test_backend_logs_view_normalizes_timestamp_and_passes_exact_level(monkeypatch):
    captured: dict[str, object] = {}

    def fake_get_logs(**kwargs):
        captured.update(kwargs)
        return {
            "logs": [],
            "total": 0,
            "has_more": False,
            "active_filters": {
                "level": kwargs.get("level").value if kwargs.get("level") else None,
                "errors_only": True if kwargs.get("errors_only") else None,
                "newer_than_id": kwargs.get("newer_than_id"),
                "older_than_id": kwargs.get("older_than_id"),
                "operation_id": kwargs.get("operation_id"),
                "since": kwargs.get("since").isoformat() if kwargs.get("since") else None,
            },
        }

    monkeypatch.setattr(admin_logs, "get_logs", fake_get_logs)

    since = datetime(2026, 4, 3, 1, 2, 3)
    result = await admin_logs.get_backend_logs(
        level=LogLevel.INFO,
        limit=10,
        offset=3,
        since=since,
        operation_id="op-123",
    )

    assert captured["level"] == LogLevel.INFO
    assert captured["errors_only"] is False
    assert captured["newer_than_id"] is None
    assert captured["older_than_id"] is None
    assert captured["limit"] == 10
    assert captured["offset"] == 3
    assert captured["operation_id"] == "op-123"
    assert captured["since"].tzinfo == timezone.utc
    assert result["active_filters"]["level"] == "INFO"
    assert result["active_filters"]["errors_only"] is None
    assert result["active_filters"]["operation_id"] == "op-123"
    assert result["active_filters"]["since"].endswith("+00:00")


@pytest.mark.asyncio
async def test_backend_logs_view_forwards_errors_only_filter(monkeypatch):
    captured: dict[str, object] = {}

    def fake_get_logs(**kwargs):
        captured.update(kwargs)
        return {
            "logs": [],
            "total": 0,
            "has_more": False,
            "active_filters": {
                "level": kwargs.get("level").value if kwargs.get("level") else None,
                "errors_only": True if kwargs.get("errors_only") else None,
                "newer_than_id": kwargs.get("newer_than_id"),
                "older_than_id": kwargs.get("older_than_id"),
                "operation_id": kwargs.get("operation_id"),
                "since": kwargs.get("since").isoformat() if kwargs.get("since") else None,
            },
        }

    monkeypatch.setattr(admin_logs, "get_logs", fake_get_logs)

    result = await admin_logs.get_backend_logs(errors_only=True)

    assert captured["errors_only"] is True
    assert captured["level"] is None
    assert result["active_filters"]["errors_only"] is True


@pytest.mark.asyncio
async def test_backend_logs_view_forwards_id_cursors(monkeypatch):
    captured: dict[str, object] = {}

    def fake_get_logs(**kwargs):
        captured.update(kwargs)
        return {
            "logs": [],
            "total": 0,
            "has_more": False,
            "active_filters": {
                "level": kwargs.get("level").value if kwargs.get("level") else None,
                "errors_only": True if kwargs.get("errors_only") else None,
                "newer_than_id": kwargs.get("newer_than_id"),
                "older_than_id": kwargs.get("older_than_id"),
                "operation_id": kwargs.get("operation_id"),
                "since": kwargs.get("since").isoformat() if kwargs.get("since") else None,
            },
        }

    monkeypatch.setattr(admin_logs, "get_logs", fake_get_logs)

    result = await admin_logs.get_backend_logs(newer_than_id=10, older_than_id=5)

    assert captured["newer_than_id"] == 10
    assert captured["older_than_id"] == 5
    assert result["active_filters"]["newer_than_id"] == 10
    assert result["active_filters"]["older_than_id"] == 5


@pytest.mark.asyncio
async def test_backend_logs_route_rejects_invalid_level(test_api_key):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Proxbox-API-Key": test_api_key},
    ) as client:
        response = await client.get("/admin/logs?level=bogus")

    assert response.status_code == 422
