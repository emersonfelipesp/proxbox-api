"""Tests for health and metadata endpoints."""

from __future__ import annotations

import pytest

from proxbox_api import settings_client
from proxbox_api.app import bootstrap
from proxbox_api.app.root_meta import health_check


@pytest.mark.asyncio
async def test_health_check_reads_live_bootstrap_state(monkeypatch):
    monkeypatch.setattr(bootstrap, "init_ok", False)
    assert await health_check() == {"status": "initializing", "init_ok": False}

    monkeypatch.setattr(bootstrap, "init_ok", True)
    assert await health_check() == {"status": "ready", "init_ok": True}


def test_backend_version_advertises_websocket_run_correlation(auth_test_client):
    response = auth_test_client.get("/version")

    assert response.status_code == 200
    assert response.json()["capabilities"] == {"ws_run_id": 1}


def test_backend_version_authentication_is_unchanged(test_client):
    assert test_client.get("/version").status_code == 401


@pytest.mark.asyncio
async def test_health_check_fails_when_settings_policy_access_is_denied(monkeypatch):
    monkeypatch.setattr(bootstrap, "init_ok", True)
    monkeypatch.setattr(
        settings_client,
        "_SETTINGS_AUTHORIZATION_DENIED_WITHOUT_POLICY",
        True,
    )

    response = await health_check()

    assert response.status_code == 503
    assert b"view_proxboxpluginsettings" in response.body


@pytest.mark.asyncio
async def test_readiness_probe_recovers_after_settings_permission_is_repaired(monkeypatch):
    settings_client.invalidate_settings_cache(clear_readiness=True)
    monkeypatch.setattr(bootstrap, "init_ok", True)
    monkeypatch.setattr(settings_client, "_settings_session", lambda _session: object())
    now = [100.0]
    permission_granted = False
    calls = 0

    def fetch(_session, **_kwargs):
        nonlocal calls
        calls += 1
        if not permission_granted:
            raise settings_client.SettingsAuthorizationError("HTTP 403")
        settings = settings_client.get_default_settings()
        settings["allow_private_ips"] = False
        return settings

    monkeypatch.setattr(settings_client.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(settings_client, "fetch_settings_from_netbox", fetch)
    settings_client.get_settings(use_cache=False)

    denied = await health_check()
    assert denied.status_code == 503
    assert calls == 1

    permission_granted = True
    now[0] += settings_client._SETTINGS_FAILURE_TTL + 0.1
    recovered = await health_check()

    assert recovered == {"status": "ready", "init_ok": True}
    assert calls == 2
    assert settings_client.settings_readiness_failure() is None
    settings_client.invalidate_settings_cache(clear_readiness=True)


def test_health_route_surfaces_settings_policy_denial(test_client, monkeypatch):
    monkeypatch.setattr(bootstrap, "init_ok", True)
    monkeypatch.setattr(
        settings_client,
        "_SETTINGS_AUTHORIZATION_DENIED_WITHOUT_POLICY",
        True,
    )

    response = test_client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unready",
        "init_ok": True,
        "reason": (
            "NetBox denied plugin settings access; grant view_proxboxpluginsettings "
            "to the proxbox-api service account."
        ),
    }
