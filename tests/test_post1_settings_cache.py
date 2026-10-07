"""Failed settings fetches must not replace the operator's policy with defaults."""

from __future__ import annotations

import pytest

from proxbox_api import settings_client


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    settings_client.invalidate_settings_cache()
    monkeypatch.setattr(settings_client, "_settings_session", lambda s: object())
    yield
    settings_client.invalidate_settings_cache()


def _good() -> dict:
    settings = settings_client.get_default_settings()
    settings["allow_private_ips"] = False
    settings["blocked_ip_ranges"] = ["10.0.0.0/8"]
    return settings


def _script(monkeypatch, results: list):
    calls = {"n": 0}

    def fake(session, **kwargs):
        value = results[min(calls["n"], len(results) - 1)]
        calls["n"] += 1
        return value

    monkeypatch.setattr(settings_client, "fetch_settings_from_netbox", fake)
    return calls


def test_failed_fetch_serves_last_good_and_is_not_cached_as_fetched(monkeypatch):
    calls = _script(monkeypatch, [_good(), None, _good()])
    first = settings_client.get_settings(use_cache=False)
    assert first["allow_private_ips"] is False
    second = settings_client.get_settings(use_cache=False)
    assert second["allow_private_ips"] is False
    assert second["blocked_ip_ranges"] == ["10.0.0.0/8"]
    assert settings_client._SETTINGS_LAST_RESULT["allow_private_ips"] is False
    # Failure cache entry expires within the short retry window, not the full TTL.
    age = settings_client.time.monotonic() - settings_client._SETTINGS_CACHE_TIME
    assert age >= settings_client._SETTINGS_CACHE_TTL - settings_client._SETTINGS_FAILURE_TTL - 1
    assert calls["n"] == 2


def test_no_last_good_uses_defaults_with_short_ttl_and_retries(monkeypatch):
    calls = _script(monkeypatch, [None, _good()])
    first = settings_client.get_settings()
    assert first["allow_private_ips"] is True  # defaults
    assert settings_client._SETTINGS_LAST_RESULT is None
    # Within the short window the cached fallback is reused.
    settings_client.get_settings()
    assert calls["n"] == 1
    # Once the short TTL elapses the next call retries and recovers.
    settings_client._SETTINGS_CACHE_TIME -= settings_client._SETTINGS_FAILURE_TTL + 1
    recovered = settings_client.get_settings()
    assert calls["n"] == 2
    assert recovered["allow_private_ips"] is False
    assert settings_client._SETTINGS_LAST_RESULT is not None


def test_failure_does_not_extend_to_full_ttl(monkeypatch):
    _script(monkeypatch, [None])
    settings_client.get_settings()
    remaining = settings_client._SETTINGS_CACHE_TTL - (
        settings_client.time.monotonic() - settings_client._SETTINGS_CACHE_TIME
    )
    assert remaining <= settings_client._SETTINGS_FAILURE_TTL + 0.5


def test_cache_fallback_false_does_not_cache_failure(monkeypatch):
    calls = _script(monkeypatch, [None, None])
    settings_client.get_settings(cache_fallback=False)
    settings_client.get_settings(cache_fallback=False)
    assert calls["n"] == 2
