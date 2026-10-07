"""SSRF hardening: IPv4-mapped normalization, fail-closed literals, loader logging."""

from __future__ import annotations

import logging

import pytest

from proxbox_api import ssrf


@pytest.fixture(autouse=True)
def _clean_cache():
    ssrf.clear_endpoint_cache()
    yield
    ssrf.clear_endpoint_cache()


@pytest.fixture
def no_registered(monkeypatch):
    monkeypatch.setattr(ssrf, "get_registered_endpoints", lambda: (set(), set()))


SETTINGS = {"allow_private_ips": False}


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "::ffff:10.0.0.5",
        "::ffff:192.168.1.1",
        "2002:7f00:0001::",  # 6to4 wrapping 127.0.0.1
    ],
)
def test_internal_and_mapped_blocked(no_registered, ip):
    blocked, reason = ssrf.is_ip_blocked(ip, SETTINGS)
    assert blocked, reason


@pytest.mark.parametrize("ip", ["8.8.8.8", "::ffff:8.8.8.8", "2606:4700:4700::1111"])
def test_public_allowed(no_registered, ip):
    assert ssrf.is_ip_blocked(ip, SETTINGS) == (False, "OK")


def test_private_mapped_allowed_when_private_permitted(no_registered):
    assert ssrf.is_ip_blocked("::ffff:10.0.0.5", {"allow_private_ips": True}) == (False, "OK")


@pytest.mark.parametrize("host", ["999.1.1.1", "1.2.3", "[::1]", "gggg::1"])
def test_unparseable_literal_fails_closed(no_registered, host):
    blocked, reason = ssrf.is_ip_blocked(host, SETTINGS)
    assert blocked and "could not be parsed" in reason


def test_zone_id_literal_blocked(no_registered):
    # Python 3.13+ parses zone ids; older versions hit the fail-closed branch.
    assert ssrf.is_ip_blocked("fe80::1%eth0", SETTINGS)[0]


def test_hostname_not_treated_as_literal(no_registered):
    assert ssrf.is_ip_blocked("netbox.example.com", SETTINGS) == (False, "OK")


def test_loader_logs_and_does_not_cache_on_db_failure(monkeypatch, caplog):
    import proxbox_api.database as database

    def boom():
        raise RuntimeError("secret-dsn")

    monkeypatch.setattr(database, "get_engine", boom)
    logger = logging.getLogger("proxbox")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="proxbox"):
            assert ssrf.get_registered_endpoints() == (set(), set())
    finally:
        logger.removeHandler(caplog.handler)
    assert any("could not load registered endpoints" in r.getMessage() for r in caplog.records)
    assert not any("secret-dsn" in r.getMessage() for r in caplog.records)
    assert ssrf._registered_ips_cache == set()
    assert ssrf._registered_domains_cache == set()


_DENY_CASES = [
    ("mapped", "::ffff:8.8.8.8", "::ffff:0:0/96"),
    ("6to4", "2002:0808:0808::1", "2002::/16"),
    ("teredo", "2001:0:4136:e378:8000:1234:f7f7:f7f7", "2001::/32"),
]


@pytest.mark.parametrize(("kind", "ip", "deny"), _DENY_CASES, ids=[c[0] for c in _DENY_CASES])
def test_explicit_ipv6_deny_rules_still_apply_to_embedded_forms(no_registered, kind, ip, deny):
    import ipaddress

    open_settings = {"allow_private_ips": False}
    blocked, reason = ssrf.is_ip_blocked(ip, open_settings)
    assert not blocked, f"control {kind}: embedded public IPv4 should be allowed ({reason})"
    deny_settings = {**open_settings, "blocked_ip_ranges": [ipaddress.ip_network(deny)]}
    blocked, reason = ssrf.is_ip_blocked(ip, deny_settings)
    assert blocked and "explicitly blocked" in reason


def test_ipv4_allow_range_still_covers_mapped_private_address(no_registered):
    import ipaddress

    settings = {
        "allow_private_ips": False,
        "allowed_ip_ranges": [ipaddress.ip_network("10.0.0.0/8")],
    }
    assert ssrf.is_ip_blocked("::ffff:10.0.0.5", settings) == (False, "OK")
    assert ssrf.is_ip_blocked("::ffff:192.168.1.1", settings)[0]


def test_ipv4_allow_range_does_not_whitelist_relay_embedded_forms(no_registered):
    """6to4 and Teredo only embed an IPv4; an IPv4 allow range must not admit them."""
    import ipaddress

    settings = {
        "allow_private_ips": False,
        "allowed_ip_ranges": [ipaddress.ip_network("10.0.0.0/8")],
    }
    assert ssrf.is_ip_blocked("::ffff:10.0.0.5", settings) == (False, "OK")  # true mapped form
    assert ssrf.is_ip_blocked("2002:0a00:0005::", settings)[0]  # 6to4 of 10.0.0.5
