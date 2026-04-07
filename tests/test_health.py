"""Tests for health and metadata endpoints."""

from __future__ import annotations

import pytest

from proxbox_api.app import bootstrap
from proxbox_api.app.root_meta import health_check


@pytest.mark.asyncio
async def test_health_check_reads_live_bootstrap_state(monkeypatch):
    monkeypatch.setattr(bootstrap, "init_ok", False)
    assert await health_check() == {"status": "initializing", "init_ok": False}

    monkeypatch.setattr(bootstrap, "init_ok", True)
    assert await health_check() == {"status": "ready", "init_ok": True}
