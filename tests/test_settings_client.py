"""Tests for plugin settings fetch and fallback behavior."""

from __future__ import annotations

from proxbox_api import settings_client


def test_get_default_settings_exposes_backend_log_file_path():
    settings = settings_client.get_default_settings()
    assert settings["backend_log_file_path"] == "/var/log/proxbox.log"


def test_fetch_settings_from_netbox_reads_backend_log_file_path():
    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "backend_log_file_path": "/srv/log/proxbox-api.log",
                "ssrf_protection_enabled": True,
                "allow_private_ips": True,
                "additional_allowed_ip_ranges": "",
                "explicitly_blocked_ip_ranges": "",
            }

    class _Session:
        class http_session:  # noqa: N801
            @staticmethod
            def get(path, timeout=10):
                assert path == "/api/plugins/proxbox/settings/"
                return _Response()

    settings = settings_client.fetch_settings_from_netbox(_Session())
    assert settings is not None
    assert settings["backend_log_file_path"] == "/srv/log/proxbox-api.log"


def test_fetch_settings_from_netbox_falls_back_for_invalid_backend_log_file_path():
    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "backend_log_file_path": "relative/path.log",
                "ssrf_protection_enabled": True,
                "allow_private_ips": True,
                "additional_allowed_ip_ranges": "",
                "explicitly_blocked_ip_ranges": "",
            }

    class _Session:
        class http_session:  # noqa: N801
            @staticmethod
            def get(path, timeout=10):
                assert path == "/api/plugins/proxbox/settings/"
                return _Response()

    settings = settings_client.fetch_settings_from_netbox(_Session())
    assert settings is not None
    assert settings["backend_log_file_path"] == "/var/log/proxbox.log"


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
