"""Credential fetch transport tests.

Verifies :func:`proxbox_api.services.hardware_discovery.fetch_credential`:

- builds the netbox-proxbox plugin URL correctly,
- sets the ``Authorization`` header from the NetBox session,
- maps HTTP 404 to :class:`MissingCredential`,
- maps non-200 / malformed JSON / non-dict bodies to
  :class:`HardwareDiscoveryError`,
- does not log payload bodies (no secret leakage).
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from proxbox_api.services import hardware_discovery


def _make_session(base_url: str = "https://netbox.example/", token: str = "abc123") -> MagicMock:
    session = MagicMock()
    session.client.config.base_url = base_url
    session.client.config.token = token
    session.client.config.ssl_verify = True
    return session


def _make_response(status: int, body: bytes) -> Any:
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__ = lambda self=resp: self
    resp.__exit__ = lambda *_a: None
    return resp


def test_success_returns_credential() -> None:
    session = _make_session()
    payload = {
        "node_id": 7,
        "username": "proxbox-discovery",
        "port": 22,
        "known_host_fingerprint": "SHA256:" + "B" * 43,
        "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n…\n-----END OPENSSH PRIVATE KEY-----",
        "password": None,
        "sudo_required": True,
    }
    response = _make_response(200, json.dumps(payload).encode())

    with patch("urllib.request.urlopen", return_value=response) as urlopen:
        cred = hardware_discovery.fetch_credential(session, 7, "10.0.0.1")

    request = urlopen.call_args[0][0]
    assert request.full_url == (
        "https://netbox.example/api/plugins/proxbox/ssh-credentials/by-node/7/credentials/"
    )
    assert request.headers["Authorization"]
    assert cred.username == "proxbox-discovery"
    assert cred.node_id == 7
    assert cred.host == "10.0.0.1"
    assert cred.sudo_required is True


def test_404_raises_missing_credential() -> None:
    session = _make_session()
    err = urllib.error.HTTPError(url="x", code=404, msg="Not Found", hdrs=None, fp=io.BytesIO(b""))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(hardware_discovery.MissingCredential):
            hardware_discovery.fetch_credential(session, 7, "10.0.0.1")


def test_500_raises_hardware_discovery_error() -> None:
    session = _make_session()
    err = urllib.error.HTTPError(url="x", code=500, msg="ISE", hdrs=None, fp=io.BytesIO(b""))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(hardware_discovery.HardwareDiscoveryError):
            hardware_discovery.fetch_credential(session, 7, "10.0.0.1")


def test_malformed_json_raises() -> None:
    session = _make_session()
    response = _make_response(200, b"not-json")
    with patch("urllib.request.urlopen", return_value=response):
        with pytest.raises(hardware_discovery.HardwareDiscoveryError):
            hardware_discovery.fetch_credential(session, 7, "10.0.0.1")


def test_non_dict_body_raises() -> None:
    session = _make_session()
    response = _make_response(200, b"[]")
    with patch("urllib.request.urlopen", return_value=response):
        with pytest.raises(hardware_discovery.HardwareDiscoveryError):
            hardware_discovery.fetch_credential(session, 7, "10.0.0.1")


def test_missing_fingerprint_raises() -> None:
    session = _make_session()
    payload = {"username": "u", "known_host_fingerprint": "  "}
    response = _make_response(200, json.dumps(payload).encode())
    with patch("urllib.request.urlopen", return_value=response):
        with pytest.raises(hardware_discovery.HardwareDiscoveryError):
            hardware_discovery.fetch_credential(session, 7, "10.0.0.1")


def test_missing_base_url_raises() -> None:
    session = _make_session(base_url="")
    with pytest.raises(hardware_discovery.HardwareDiscoveryError):
        hardware_discovery.fetch_credential(session, 7, "10.0.0.1")


def test_secrets_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    session = _make_session()
    secret_key = "VERY-SECRET-KEY-MATERIAL-XYZ"
    payload = {
        "node_id": 7,
        "username": "u",
        "known_host_fingerprint": "SHA256:" + "C" * 43,
        "private_key": secret_key,
    }
    response = _make_response(200, json.dumps(payload).encode())
    caplog.set_level("DEBUG")
    with patch("urllib.request.urlopen", return_value=response):
        hardware_discovery.fetch_credential(session, 7, "10.0.0.1")

    for record in caplog.records:
        assert secret_key not in record.getMessage()
