"""Security regression tests for codegen SSRF guard and apidoc SSL fallback."""

from __future__ import annotations

import logging
import ssl
from urllib.error import URLError

import pytest

from proxbox_api.exception import ProxboxException
from proxbox_api.proxmox_codegen import apidoc_parser
from proxbox_api.routes.proxmox import viewer_codegen


def test_apidoc_fetch_raises_on_ssl_error_by_default(monkeypatch):
    def _raise(*_args, **_kwargs):
        raise URLError(reason=ssl.SSLError("self-signed certificate"))

    monkeypatch.setattr(apidoc_parser, "urlopen", _raise)

    with pytest.raises(URLError):
        apidoc_parser.fetch_apidoc_js("https://example.invalid/apidoc.js")


def test_apidoc_fetch_allows_insecure_with_explicit_opt_in(monkeypatch, caplog):
    calls: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self) -> bytes:
            return b"const apiSchema = [];"

    def _urlopen(url, timeout=60, context=None):
        calls.append({"url": url, "context": context})
        if context is None:
            raise URLError(reason=ssl.SSLError("self-signed certificate"))
        return _FakeResponse()

    monkeypatch.setattr(apidoc_parser, "urlopen", _urlopen)

    with caplog.at_level(logging.CRITICAL, logger=apidoc_parser.logger.name):
        body = apidoc_parser.fetch_apidoc_js(
            "https://lab.invalid/apidoc.js",
            allow_insecure_ssl=True,
        )

    assert body.startswith("const apiSchema")
    assert any("SSL verification disabled" in rec.message for rec in caplog.records)
    assert calls[0]["context"] is None
    assert isinstance(calls[1]["context"], ssl.SSLContext)


def test_apidoc_fetch_reraises_non_ssl_url_error(monkeypatch):
    def _raise(*_args, **_kwargs):
        raise URLError(reason=ConnectionRefusedError("nope"))

    monkeypatch.setattr(apidoc_parser, "urlopen", _raise)

    with pytest.raises(URLError):
        apidoc_parser.fetch_apidoc_js(
            "https://example.invalid/apidoc.js",
            allow_insecure_ssl=True,
        )


@pytest.mark.parametrize(
    "blocked_url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/internal",
        "http://localhost/",
    ],
)
def test_codegen_source_url_rejects_internal_hosts(monkeypatch, blocked_url):
    """Internal/reserved hosts must be refused before any HTTP request fires."""

    monkeypatch.setattr(
        viewer_codegen,
        "get_settings",
        lambda *_a, **_kw: {
            "ssrf_protection_enabled": True,
            "allow_private_ips": False,
            "allowed_ip_ranges": [],
            "blocked_ip_ranges": [],
        },
    )

    with pytest.raises(ProxboxException) as exc_info:
        viewer_codegen._enforce_codegen_source_url(blocked_url)

    assert "source_url is not allowed" in exc_info.value.message


def test_codegen_source_url_accepts_default_proxmox_viewer(monkeypatch):
    """Default upstream Proxmox viewer must remain reachable after guard."""

    monkeypatch.setattr(
        viewer_codegen,
        "get_settings",
        lambda *_a, **_kw: {
            "ssrf_protection_enabled": True,
            "allow_private_ips": False,
            "allowed_ip_ranges": [],
            "blocked_ip_ranges": [],
        },
    )

    viewer_codegen._enforce_codegen_source_url("https://pve.proxmox.com/pve-docs/api-viewer/")
