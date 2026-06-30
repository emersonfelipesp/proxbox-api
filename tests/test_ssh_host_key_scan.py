"""Tests for the SSH host-key fingerprint scan endpoint and service.

The scan must mirror the browser-terminal connection EXACTLY so the fetched
fingerprint equals what the pinned-fingerprint check verifies on a real
session. These tests pin that invariant and the capture/normalize behavior.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

import proxbox_api.services.ssh_terminal as ssh_terminal
from proxbox_api.services.ssh_terminal import (
    HostKeyScanError,
    scan_host_key_fingerprint,
)

# Canonical fingerprint = 43 base64 chars, no padding. The fake key returns the
# padded form to prove _canonical_fingerprint strips the trailing "=".
_FP_BODY = "A" * 43


class _FakeKey:
    def get_fingerprint(self, algorithm: str = "sha256") -> str:
        assert algorithm in ("sha256", "SHA256")
        return f"SHA256:{_FP_BODY}="

    def get_algorithm(self) -> bytes:
        return b"ssh-ed25519"


class _FakePermissionDenied(Exception):
    pass


class _FakeSSHClient:
    pass


def _fake_asyncssh(*, on_connect_record: dict | None = None) -> object:
    fake = types.SimpleNamespace()
    fake.SSHClient = _FakeSSHClient
    fake.PermissionDenied = _FakePermissionDenied
    fake.Error = Exception

    async def connect(host, **kwargs):  # noqa: ANN001, ANN003
        if on_connect_record is not None:
            on_connect_record.update(kwargs)
        # Mirror real asyncssh: validate_host_public_key fires during key
        # exchange (before auth). Instantiate the client factory class, run the
        # callback with a fake server key, then fail auth.
        client = kwargs["client_factory"]()
        client.validate_host_public_key(host, None, kwargs["port"], _FakeKey())
        raise fake.PermissionDenied("authentication failed (expected in scan)")

    fake.connect = connect
    return fake


# ---------------------------------------------------------------------------
# Source contract — the scan must mirror the terminal connect args
# ---------------------------------------------------------------------------


def test_scan_mirrors_terminal_host_key_args() -> None:
    source = Path("proxbox_api/services/ssh_terminal.py").read_text()
    scan_src = source.split("async def scan_host_key_fingerprint", 1)[1].split(
        "\nasync def _maybe_drain", 1
    )[0]
    assert 'known_hosts=b""' in scan_src
    assert 'server_host_key_algs="default"' in scan_src
    # known_hosts=None disables the callback entirely -> nothing captured.
    assert "known_hosts=None" not in scan_src


# ---------------------------------------------------------------------------
# Behavior — capture + canonical fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_returns_canonical_fingerprint_and_key_type(monkeypatch) -> None:
    record: dict = {}
    monkeypatch.setattr(
        ssh_terminal,
        "_load_asyncssh",
        lambda: _fake_asyncssh(on_connect_record=record),
    )

    fingerprint, key_type = await scan_host_key_fingerprint("10.0.0.9", 22)

    # Padding stripped, prefix preserved — exactly what the verifier compares.
    assert fingerprint == f"SHA256:{_FP_BODY}"
    assert key_type == "ssh-ed25519"
    # The connect call used the terminal-matching host-key args.
    assert record["known_hosts"] == b""
    assert record["server_host_key_algs"] == "default"


@pytest.mark.asyncio
async def test_scan_raises_when_no_key_captured(monkeypatch) -> None:
    fake = types.SimpleNamespace()
    fake.SSHClient = _FakeSSHClient
    fake.PermissionDenied = _FakePermissionDenied
    fake.Error = Exception

    async def connect(host, **kwargs):  # noqa: ANN001, ANN003 — connection never reaches key exchange
        raise OSError("connection refused")

    fake.connect = connect
    monkeypatch.setattr(ssh_terminal, "_load_asyncssh", lambda: fake)

    with pytest.raises(HostKeyScanError):
        await scan_host_key_fingerprint("10.0.0.9", 22)


# ---------------------------------------------------------------------------
# Route — auth gate + success + upstream error mapping
# ---------------------------------------------------------------------------


def test_host_key_fingerprint_requires_backend_api_key(test_client) -> None:
    response = test_client.get("/ssh/host-key-fingerprint", params={"host": "10.0.0.9"})
    assert response.status_code == 401


def test_host_key_fingerprint_returns_payload(auth_test_client, monkeypatch) -> None:
    async def fake_scan(host, port=22, *, timeout=10.0):  # noqa: ANN001, ANN201
        return f"SHA256:{_FP_BODY}", "ssh-ed25519"

    monkeypatch.setattr("proxbox_api.routes.ssh_terminal.scan_host_key_fingerprint", fake_scan)
    response = auth_test_client.get(
        "/ssh/host-key-fingerprint", params={"host": "10.0.0.9", "port": 22}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["fingerprint"] == f"SHA256:{_FP_BODY}"
    assert payload["key_type"] == "ssh-ed25519"
    assert payload["host"] == "10.0.0.9"
    assert payload["port"] == 22


def test_host_key_fingerprint_maps_scan_error_to_502(auth_test_client, monkeypatch) -> None:
    async def fake_scan(host, port=22, *, timeout=10.0):  # noqa: ANN001, ANN201
        raise HostKeyScanError("Could not retrieve SSH host key")

    monkeypatch.setattr("proxbox_api.routes.ssh_terminal.scan_host_key_fingerprint", fake_scan)
    response = auth_test_client.get("/ssh/host-key-fingerprint", params={"host": "10.0.0.9"})
    assert response.status_code == 502
