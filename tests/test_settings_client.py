"""Tests for plugin settings fetch and fallback behavior."""

from __future__ import annotations

from proxbox_api import settings_client


def test_get_default_settings_exposes_backend_log_file_path():
    settings = settings_client.get_default_settings()
    assert settings["backend_log_file_path"] == "/var/log/proxbox.log"
    assert settings["primary_ip_preference"] == "ipv4"
    assert settings["encryption_key"] == ""


def test_fetch_settings_from_netbox_reads_backend_log_file_path(monkeypatch):
    import json
    from unittest.mock import MagicMock

    class _Config:
        base_url = "https://netbox.local"
        token_secret = "test-token"
        token_version = "v1"
        token_key = None

    class _Client:
        config = _Config()

    class _Session:
        client = _Client()

    response_data = {
        "backend_log_file_path": "/srv/log/proxbox-api.log",
        "primary_ip_preference": "ipv6",
        "ssrf_protection_enabled": True,
        "allow_private_ips": True,
        "additional_allowed_ip_ranges": "",
        "explicitly_blocked_ip_ranges": "",
        "encryption_key": "my-plugin-key",
    }

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(response_data).encode()
    mock_response.status = 200
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: mock_response)

    settings = settings_client.fetch_settings_from_netbox(_Session())
    assert settings is not None
    assert settings["backend_log_file_path"] == "/srv/log/proxbox-api.log"
    assert settings["primary_ip_preference"] == "ipv6"
    assert settings["encryption_key"] == "my-plugin-key"


def test_fetch_settings_from_netbox_falls_back_for_invalid_backend_log_file_path(monkeypatch):
    import json
    from unittest.mock import MagicMock

    class _Config:
        base_url = "https://netbox.local"
        token_secret = "test-token"
        token_version = "v1"
        token_key = None

    class _Client:
        config = _Config()

    class _Session:
        client = _Client()

    response_data = {
        "backend_log_file_path": "relative/path.log",
        "primary_ip_preference": "not-valid",
        "ssrf_protection_enabled": True,
        "allow_private_ips": True,
        "additional_allowed_ip_ranges": "",
        "explicitly_blocked_ip_ranges": "",
    }

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(response_data).encode()
    mock_response.status = 200
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: mock_response)

    settings = settings_client.fetch_settings_from_netbox(_Session())
    assert settings is not None
    assert settings["backend_log_file_path"] == "/var/log/proxbox.log"
    assert settings["primary_ip_preference"] == "ipv4"


def test_get_settings_uses_raw_netbox_session_when_no_session(monkeypatch):
    sentinel = object()

    monkeypatch.setattr(
        "proxbox_api.settings_client.get_default_settings",
        lambda: {"fallback": True},
    )
    monkeypatch.setattr(
        "proxbox_api.app.netbox_session.get_raw_netbox_session",
        lambda: sentinel,
    )
    monkeypatch.setattr(
        "proxbox_api.settings_client.fetch_settings_from_netbox",
        lambda session: {"ok": session is sentinel},
    )

    settings_client.invalidate_settings_cache()
    result = settings_client.get_settings(netbox_session=None, use_cache=False)
    assert result == {"ok": True}


def test_get_settings_falls_back_when_raw_session_unavailable(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.settings_client.get_default_settings",
        lambda: {"fallback": True},
    )
    monkeypatch.setattr(
        "proxbox_api.app.netbox_session.get_raw_netbox_session",
        lambda: None,
    )

    settings_client.invalidate_settings_cache()
    result = settings_client.get_settings(netbox_session=None, use_cache=False)
    assert result == {"fallback": True}
