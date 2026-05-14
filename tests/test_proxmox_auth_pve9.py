"""Mock-driven Proxmox VE 9 auth regression tests.

Covers the three compounding bugs documented in
``emersonfelipesp/netbox-proxbox#417``:

* the abandoned ``aiohttp.ClientSession`` is closed on every retry path
  in :meth:`ProxmoxSession._auth_async`;
* a 401 from PVE 9 surfaces the upstream status, body, and structured
  ``errors`` dict in :class:`~proxbox_api.exception.ProxboxException`
  instead of ``"Unknown error."``;
* the session reports the PVE 9 version when the version probe succeeds.
"""

from __future__ import annotations

import pytest
from proxmox_sdk.sdk.exceptions import ResourceException

from proxbox_api.exception import ProxboxException
from proxbox_api.session.proxmox import ProxmoxSession


class FakeVersionResource:
    def __init__(self, payload):
        self.payload = payload

    def get(self):
        return self.payload


class FakeClusterStatusResource:
    def get(self):
        return [
            {"type": "cluster", "name": "pve9-lab"},
            {"type": "node", "name": "pve9-node01"},
        ]


class FakeJoinResource:
    def get(self):
        return {"nodelist": [{"pve_fp": "PVE9-FINGERPRINT"}]}


class FakePve9ProxmoxAPI:
    """SDK fake that advertises PVE 9.1.11 and records close() calls."""

    instances: list[FakePve9ProxmoxAPI] = []

    def __init__(self, host, **kwargs):
        self.host = host
        self.kwargs = kwargs
        self.closed = False
        self.version = FakeVersionResource(
            {"version": "9.1.11", "release": "9.1-11", "repoid": "abc123"}
        )
        FakePve9ProxmoxAPI.instances.append(self)

    def __call__(self, path):
        if path == "cluster/status":
            return FakeClusterStatusResource()
        if path == "cluster/config/join":
            return FakeJoinResource()
        raise AssertionError(f"unexpected path {path}")

    async def close(self):
        self.closed = True


class FakePve9AuthRejectedAPI(FakePve9ProxmoxAPI):
    """SDK fake that raises a PVE 9 styled 401 on the version probe."""

    def __init__(self, host, **kwargs):
        super().__init__(host, **kwargs)
        self.version = self  # raise on ``.get()``

    def get(self):
        raise ResourceException(
            status_code=401,
            status_message="Authentication failed",
            content='{"data":null,"errors":{"PVE":"Authentication failed"}}',
            errors={"PVE": "Authentication failed"},
        )


class FakeDomainFailsThenIpSucceedsAPI(FakePve9ProxmoxAPI):
    """Domain attempt raises so the IP attempt fallback runs."""

    def __init__(self, host, **kwargs):
        super().__init__(host, **kwargs)
        if host == "pve9.example.com":
            self.version = self  # raise on ``.get()`` for the domain attempt

    def get(self):  # only used for the domain attempt
        raise RuntimeError("simulated domain DNS failure")


@pytest.fixture(autouse=True)
def reset_instances():
    FakePve9ProxmoxAPI.instances.clear()
    yield
    FakePve9ProxmoxAPI.instances.clear()


def test_session_reports_pve9_version_via_token_auth(monkeypatch):
    monkeypatch.setattr("proxbox_api.session.proxmox.ProxmoxAPI", FakePve9ProxmoxAPI)

    session = ProxmoxSession(
        {
            "ip_address": "10.0.30.10",
            "domain": "pve9.example.com",
            "http_port": 8006,
            "user": "root@pam",
            "password": None,
            "token": {"name": "proxbox", "value": "pve9-secret"},
            "ssl": False,
        }
    )

    assert session.CONNECTED is True
    assert session.version == {
        "version": "9.1.11",
        "release": "9.1-11",
        "repoid": "abc123",
    }
    assert session.name == "pve9-lab"


def test_auth_failure_surfaces_upstream_pve_error(monkeypatch):
    monkeypatch.setattr("proxbox_api.session.proxmox.ProxmoxAPI", FakePve9AuthRejectedAPI)

    with pytest.raises(ProxboxException) as exc_info:
        ProxmoxSession(
            {
                "ip_address": "10.0.30.10",
                "domain": None,
                "http_port": 8006,
                "user": "root@pam",
                "password": None,
                "token": {"name": "proxbox", "value": "stale-token"},
                "ssl": False,
            }
        )

    detail = exc_info.value.detail or ""
    assert "Unknown error." not in detail
    assert "401" in detail
    assert "Authentication failed" in detail
    assert "PVE" in detail


def test_abandoned_sdk_is_closed_on_domain_fallback(monkeypatch):
    monkeypatch.setattr("proxbox_api.session.proxmox.ProxmoxAPI", FakeDomainFailsThenIpSucceedsAPI)

    session = ProxmoxSession(
        {
            "ip_address": "10.0.30.10",
            "domain": "pve9.example.com",
            "http_port": 8006,
            "user": "root@pam",
            "password": None,
            "token": {"name": "proxbox", "value": "pve9-secret"},
            "ssl": False,
        }
    )

    assert session.CONNECTED is True
    domain_attempts = [
        sdk for sdk in FakePve9ProxmoxAPI.instances if sdk.host == "pve9.example.com"
    ]
    ip_attempts = [sdk for sdk in FakePve9ProxmoxAPI.instances if sdk.host == "10.0.30.10"]
    assert len(domain_attempts) == 1, "exactly one domain attempt expected"
    assert len(ip_attempts) == 1, "exactly one IP attempt expected"
    assert domain_attempts[0].closed is True, "abandoned domain SDK must be aclose()-ed"
    assert ip_attempts[0].closed is False, "successful SDK is owned by the caller"


def test_abandoned_sdk_is_closed_when_both_attempts_fail(monkeypatch):
    monkeypatch.setattr("proxbox_api.session.proxmox.ProxmoxAPI", FakePve9AuthRejectedAPI)

    with pytest.raises(ProxboxException):
        ProxmoxSession(
            {
                "ip_address": "10.0.30.10",
                "domain": "pve9.example.com",
                "http_port": 8006,
                "user": "root@pam",
                "password": None,
                "token": {"name": "proxbox", "value": "bad-token"},
                "ssl": False,
            }
        )

    assert len(FakePve9ProxmoxAPI.instances) == 2
    assert all(sdk.closed for sdk in FakePve9ProxmoxAPI.instances)
