"""Tests for the SSH host-key fingerprint scan endpoint and service.

The scan must mirror the browser-terminal connection so the fetched fingerprint
equals what the pinned-fingerprint check verifies on a real session. The host
key is captured two ways — ``validate_host_public_key`` (when the runtime
consults it) and ``conn.get_server_host_key()`` (the deployed runtime bypasses
the callback) — and these tests pin both paths plus the route wiring.
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


class _FakeConn:
    def __init__(self, key: _FakeKey | None) -> None:
        self._key = key

    def get_server_host_key(self) -> _FakeKey | None:
        return self._key

    def close(self) -> None:
        pass


class _FakePermissionDenied(Exception):
    pass


class _FakeSSHClient:
    pass


def _fake_asyncssh(*, mode: str, record: dict | None = None) -> object:
    """Fake asyncssh whose connect() simulates one of the capture paths.

    mode="validate"    — runtime calls validate_host_public_key (dev venv path).
    mode="conn"        — runtime skips the callback; key is read from the stashed
                          connection via get_server_host_key() (deployed path).
    mode="unreachable" — connect refused before any callback (real error).
    """
    fake = types.SimpleNamespace()
    fake.SSHClient = _FakeSSHClient
    fake.PermissionDenied = _FakePermissionDenied
    fake.Error = Exception

    async def connect(host, **kwargs):  # noqa: ANN001, ANN003
        if record is not None:
            record.update(kwargs)
        if mode == "unreachable":
            raise OSError("connection refused")
        client = kwargs["client_factory"]()
        # connection_made always fires first and stashes the connection.
        client.connection_made(_FakeConn(_FakeKey()))
        if mode == "validate":
            client.validate_host_public_key(host, None, kwargs["port"], _FakeKey())
        # Auth always fails (the scan never authenticates).
        raise fake.PermissionDenied("authentication failed (expected in scan)")

    fake.connect = connect
    return fake


# ---------------------------------------------------------------------------
# Source contract — terminal-matching args + both capture paths present
# ---------------------------------------------------------------------------


def test_scan_mirrors_terminal_host_key_args_and_has_conn_fallback() -> None:
    source = Path("proxbox_api/services/ssh_terminal.py").read_text()
    scan_src = source.split("async def scan_host_key_fingerprint", 1)[1].split(
        "\nasync def _maybe_drain", 1
    )[0]
    assert 'known_hosts=b""' in scan_src
    assert 'server_host_key_algs="default"' in scan_src
    assert "known_hosts=None" not in scan_src
    # Robust capture: callback path + connection fallback.
    assert "validate_host_public_key" in source
    assert "get_server_host_key" in scan_src
    assert "connection_made" in source


# ---------------------------------------------------------------------------
# Behavior — both capture paths yield the canonical fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_captures_via_validate_callback(monkeypatch) -> None:
    record: dict = {}
    monkeypatch.setattr(
        ssh_terminal,
        "_load_asyncssh",
        lambda: _fake_asyncssh(mode="validate", record=record),
    )
    fingerprint, key_type = await scan_host_key_fingerprint("10.0.0.9", 22)
    assert fingerprint == f"SHA256:{_FP_BODY}"
    assert key_type == "ssh-ed25519"
    assert record["known_hosts"] == b""
    assert record["server_host_key_algs"] == "default"


@pytest.mark.asyncio
async def test_scan_captures_via_connection_when_callback_skipped(monkeypatch) -> None:
    # Mirrors the deployed runtime: validate_host_public_key is never called,
    # so the key must come from conn.get_server_host_key().
    monkeypatch.setattr(ssh_terminal, "_load_asyncssh", lambda: _fake_asyncssh(mode="conn"))
    fingerprint, key_type = await scan_host_key_fingerprint("10.0.0.9", 22)
    assert fingerprint == f"SHA256:{_FP_BODY}"
    assert key_type == "ssh-ed25519"


@pytest.mark.asyncio
async def test_scan_raises_when_host_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(ssh_terminal, "_load_asyncssh", lambda: _fake_asyncssh(mode="unreachable"))
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
