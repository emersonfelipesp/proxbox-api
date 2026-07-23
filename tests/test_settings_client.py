"""Tests for plugin settings fetch and fallback behavior."""

from __future__ import annotations

import threading
import time

from proxbox_api import runtime_settings, settings_client


def test_get_default_settings_exposes_backend_log_file_path():
    settings = settings_client.get_default_settings()
    assert settings["backend_log_file_path"] == "/var/log/proxbox.log"
    assert settings["primary_ip_preference"] == "ipv4"
    assert settings["encryption_key"] == ""
    assert settings["delete_orphans"] is False
    assert settings["reconciliation_engine"] == "python"
    assert settings["reconciliation_compare_strict"] is False
    assert settings["custom_fields_enabled"] is False
    assert settings["netbox_openapi_persist"] is True
    assert settings["cloud_network_lock_enabled"] is False
    assert settings["cloud_customer_prefix_id"] is None
    assert settings["cloud_customer_bridge"] == ""
    assert settings["cloud_customer_vlan_tag"] is None
    assert settings["cloud_customer_gateway"] == ""


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
        "delete_orphans": True,
        "reconciliation_engine": "rust",
        "reconciliation_compare_strict": True,
        "custom_fields_enabled": True,
        "cloud_network_lock_enabled": True,
        "cloud_customer_prefix_id": 321,
        "cloud_customer_bridge": "vmbr1",
        "cloud_customer_vlan_tag": 2050,
        "cloud_customer_gateway": "168.0.98.1",
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
    assert settings["delete_orphans"] is True
    assert settings["reconciliation_engine"] == "rust"
    assert settings["reconciliation_compare_strict"] is True
    assert settings["custom_fields_enabled"] is True
    assert settings["cloud_network_lock_enabled"] is True
    assert settings["cloud_customer_prefix_id"] == 321
    assert settings["cloud_customer_bridge"] == "vmbr1"
    assert settings["cloud_customer_vlan_tag"] == 2050
    assert settings["cloud_customer_gateway"] == "168.0.98.1"


def test_fetch_settings_from_netbox_reads_paginated_settings_response(monkeypatch):
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
        "count": 1,
        "next": None,
        "previous": None,
        "results": [
            {
                "backend_log_file_path": "/srv/log/proxbox-api.log",
                "primary_ip_preference": "ipv6",
                "netbox_timeout": 240,
                "netbox_get_cache_max_entries": 8192,
                "debug_cache": True,
                "delete_orphans": True,
                "reconciliation_engine": "compare",
                "reconciliation_compare_strict": True,
                "netbox_openapi_persist": False,
            }
        ],
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
    assert settings["netbox_timeout"] == 240
    assert settings["netbox_get_cache_max_entries"] == 8192
    assert settings["debug_cache"] is True
    assert settings["delete_orphans"] is True
    assert settings["reconciliation_engine"] == "compare"
    assert settings["reconciliation_compare_strict"] is True
    assert settings["netbox_openapi_persist"] is False


def test_delete_orphans_runtime_bool_prefers_env_over_settings(monkeypatch):
    monkeypatch.delenv("PROXBOX_DELETE_ORPHANS", raising=False)
    monkeypatch.setattr(
        runtime_settings,
        "_load_settings",
        lambda: {"delete_orphans": True},
    )

    assert (
        runtime_settings.get_bool(
            settings_key="delete_orphans",
            env="PROXBOX_DELETE_ORPHANS",
            default=False,
        )
        is True
    )


def test_plugin_only_runtime_helpers_ignore_env(monkeypatch):
    monkeypatch.setenv("PROXBOX_RECONCILIATION_ENGINE", "python")
    monkeypatch.setenv("PROXBOX_RECONCILIATION_COMPARE_STRICT", "false")
    monkeypatch.setattr(
        runtime_settings,
        "_load_settings",
        lambda: {
            "reconciliation_engine": "rust",
            "reconciliation_compare_strict": True,
        },
    )

    assert (
        runtime_settings.get_plugin_str(
            settings_key="reconciliation_engine",
            default="python",
        )
        == "rust"
    )
    assert (
        runtime_settings.get_plugin_bool(
            settings_key="reconciliation_compare_strict",
            default=False,
        )
        is True
    )

    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "0")
    assert (
        runtime_settings.get_bool(
            settings_key="delete_orphans",
            env="PROXBOX_DELETE_ORPHANS",
            default=True,
        )
        is False
    )

    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "1")
    assert (
        runtime_settings.get_bool(
            settings_key="delete_orphans",
            env="PROXBOX_DELETE_ORPHANS",
            default=False,
        )
        is True
    )


def test_fetch_settings_prefers_runtime_endpoint_and_falls_back_to_list(monkeypatch):
    import json
    import ssl
    import urllib.error
    from unittest.mock import MagicMock

    class _Config:
        base_url = "https://netbox.local"
        token_secret = "test-token"
        token_version = "v1"
        token_key = None
        ssl_verify = False

    class _Client:
        config = _Config()

    class _Session:
        client = _Client()

    response_data = {
        "count": 1,
        "results": [{"backend_log_file_path": "/tmp/list-fallback.log"}],
    }

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(response_data).encode()
    mock_response.status = 200
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)
    requested_urls: list[str] = []
    requested_contexts: list[object] = []

    def _urlopen(req, *args, **kwargs):
        requested_urls.append(req.full_url)
        requested_contexts.append(kwargs.get("context"))
        if req.full_url.endswith("/settings/runtime/"):
            raise urllib.error.HTTPError(
                req.full_url,
                404,
                "Not Found",
                hdrs=None,
                fp=None,
            )
        return mock_response

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)

    settings = settings_client.fetch_settings_from_netbox(_Session())
    assert settings is not None
    assert settings["backend_log_file_path"] == "/tmp/list-fallback.log"
    assert requested_urls == [
        "https://netbox.local/api/plugins/proxbox/settings/runtime/",
        "https://netbox.local/api/plugins/proxbox/settings/",
    ]
    assert len(requested_contexts) == 2
    for context in requested_contexts:
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_NONE
        assert context.check_hostname is False


def test_fetch_settings_fallback_paths_share_one_total_timeout_budget(monkeypatch):
    class _Config:
        base_url = "https://netbox.local"
        token_secret = "test-token"
        token_version = "v1"
        token_key = None
        ssl_verify = True

    class _Client:
        config = _Config()

    class _Session:
        client = _Client()

    monotonic_values = iter((100.0, 100.0, 100.3))
    request_timeouts: list[float] = []

    def _request(**kwargs):
        request_timeouts.append(kwargs["request_timeout_seconds"])
        return None, 404

    monkeypatch.setattr(settings_client.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(settings_client, "_request_settings_json", _request)

    settings = settings_client.fetch_settings_from_netbox(
        _Session(),
        request_timeout_seconds=0.5,
    )

    assert settings is None
    assert [round(timeout, 3) for timeout in request_timeouts] == [0.5, 0.2]


def test_fetch_settings_from_netbox_disables_tls_verification_when_configured(monkeypatch):
    import json
    import ssl
    from unittest.mock import MagicMock

    class _Config:
        base_url = "https://netbox.local"
        token_secret = "test-token"
        token_version = "v1"
        token_key = None
        ssl_verify = False

    class _Client:
        config = _Config()

    class _Session:
        client = _Client()

    response_data = {"backend_log_file_path": "/srv/log/proxbox-api.log"}

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(response_data).encode()
    mock_response.status = 200
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)
    urlopen_kwargs: list[dict[str, object]] = []

    def _urlopen(req, *args, **kwargs):
        urlopen_kwargs.append(kwargs)
        return mock_response

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)

    settings = settings_client.fetch_settings_from_netbox(_Session())
    assert settings is not None
    assert settings["backend_log_file_path"] == "/srv/log/proxbox-api.log"
    context = urlopen_kwargs[0].get("context")
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


def test_fetch_settings_from_netbox_keeps_default_tls_verification(monkeypatch):
    import json
    from unittest.mock import MagicMock

    class _Config:
        base_url = "https://netbox.local"
        token_secret = "test-token"
        token_version = "v1"
        token_key = None
        ssl_verify = True

    class _Client:
        config = _Config()

    class _Session:
        client = _Client()

    response_data = {"backend_log_file_path": "/srv/log/proxbox-api.log"}

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(response_data).encode()
    mock_response.status = 200
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)
    urlopen_kwargs: list[dict[str, object]] = []

    def _urlopen(req, *args, **kwargs):
        urlopen_kwargs.append(kwargs)
        return mock_response

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)

    settings = settings_client.fetch_settings_from_netbox(_Session())
    assert settings is not None
    assert settings["backend_log_file_path"] == "/srv/log/proxbox-api.log"
    assert "context" not in urlopen_kwargs[0]


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
        "reconciliation_engine": "not-valid",
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
    assert settings["reconciliation_engine"] == "python"


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


def test_bounded_settings_fallback_does_not_poison_shared_cache(monkeypatch):
    sentinel = object()
    fetch_calls = 0

    def _fetch(_session, **_kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        if fetch_calls == 1:
            return None
        return {
            "proxmox_timeout": 29,
            "proxmox_max_retries": 3,
            "proxmox_retry_backoff": 1.25,
        }

    monkeypatch.setattr(settings_client, "fetch_settings_from_netbox", _fetch)
    settings_client.invalidate_settings_cache()

    fallback = settings_client.get_settings(
        netbox_session=sentinel,
        use_cache=False,
        request_timeout_seconds=0.5,
        cache_fallback=False,
    )
    recovered = settings_client.get_settings(
        netbox_session=sentinel,
        use_cache=True,
        request_timeout_seconds=0.5,
        cache_fallback=False,
    )
    cached = settings_client.get_settings(netbox_session=sentinel, use_cache=True)

    assert fallback["proxmox_timeout"] == 5
    assert recovered["proxmox_timeout"] == 29
    assert cached["proxmox_timeout"] == 29
    assert fetch_calls == 2


def test_bounded_settings_wait_does_not_inherit_unbounded_fetch(monkeypatch):
    sentinel = object()
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    owner_result: list[dict[str, object]] = []
    fetch_calls = 0

    def _fetch(_session, **_kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        fetch_started.set()
        if not release_fetch.wait(timeout=2):
            raise TimeoutError("test did not release settings fetch")
        return {
            "proxmox_timeout": 41,
            "proxmox_max_retries": 2,
            "proxmox_retry_backoff": 0.5,
        }

    def _load_owner() -> None:
        owner_result.append(
            dict(settings_client.get_settings(netbox_session=sentinel, use_cache=True))
        )

    monkeypatch.setattr(settings_client, "fetch_settings_from_netbox", _fetch)
    settings_client.invalidate_settings_cache()
    owner = threading.Thread(target=_load_owner)
    owner.start()
    assert fetch_started.wait(timeout=1)
    started_at = time.perf_counter()
    fallback = settings_client.get_settings(
        netbox_session=sentinel,
        use_cache=True,
        request_timeout_seconds=0.05,
        cache_fallback=False,
    )
    elapsed = time.perf_counter() - started_at
    release_fetch.set()
    owner.join(timeout=2)

    assert not owner.is_alive()
    assert elapsed < 0.25
    assert fallback["proxmox_timeout"] == 5
    assert owner_result[0]["proxmox_timeout"] == 41
    assert fetch_calls == 1


def test_concurrent_cross_thread_settings_loads_share_one_result(monkeypatch):
    sentinel = object()
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    second_started = threading.Event()
    second_waiting = threading.Event()
    fetch_calls = 0
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []
    result_lock = threading.Lock()

    def _fetch(_session):
        nonlocal fetch_calls
        fetch_calls += 1
        fetch_started.set()
        if not release_fetch.wait(timeout=2):
            raise TimeoutError("test did not release settings fetch")
        return {
            "proxmox_timeout": 37,
            "proxmox_max_retries": 4,
            "proxmox_retry_backoff": 0.75,
        }

    def _load(*, mark_second: bool = False) -> None:
        if mark_second:
            second_started.set()
        try:
            result = settings_client.get_settings(
                netbox_session=sentinel,
                use_cache=True,
            )
            with result_lock:
                results.append(dict(result))
        except BaseException as error:  # pragma: no cover - assertion aid
            with result_lock:
                errors.append(error)

    monkeypatch.setattr(settings_client, "fetch_settings_from_netbox", _fetch)
    original_condition = settings_client._SETTINGS_CONDITION

    class _RecordingCondition:
        def __enter__(self):
            return original_condition.__enter__()

        def __exit__(self, *args):
            return original_condition.__exit__(*args)

        def wait(self, timeout=None):
            second_waiting.set()
            return original_condition.wait(timeout=timeout)

        def notify_all(self):
            return original_condition.notify_all()

    monkeypatch.setattr(settings_client, "_SETTINGS_CONDITION", _RecordingCondition())
    settings_client.invalidate_settings_cache()
    first = threading.Thread(target=_load)
    second = threading.Thread(target=_load, kwargs={"mark_second": True})
    first.start()
    assert fetch_started.wait(timeout=1)
    second.start()
    assert second_started.wait(timeout=1)
    assert second_waiting.wait(timeout=1)
    release_fetch.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert fetch_calls == 1
    assert len(results) == 2
    assert results[0] == results[1]
    assert results[0]["proxmox_timeout"] == 37
