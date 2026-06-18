"""Tests for POST /proxmox/console/sessions ticket-relay route."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from sqlmodel import Session

from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.exception import ProxmoxAPIError


def _make_endpoint(db_engine) -> int:
    """Insert a minimal ProxmoxEndpoint into the test DB and return its PK."""
    with Session(db_engine) as session:
        endpoint = ProxmoxEndpoint(
            name="test-pve-console",
            ip_address="10.0.0.1",
            port=8006,
            username="root@pam",
            verify_ssl=False,
        )
        session.add(endpoint)
        session.commit()
        session.refresh(endpoint)
        assert endpoint.id is not None
        return endpoint.id


class _ChainableResource:
    """Fake Proxmox SDK resource that supports the vncproxy/termproxy call chain.

    Accepts any of:
      .nodes(node).qemu(vmid).vncproxy.post(websocket=1)
      .nodes(node).lxc(vmid).vncproxy.post(websocket=1)
      .nodes(node).qemu(vmid).termproxy.post()
      .nodes(node).lxc(vmid).termproxy.post()
    """

    def __init__(self, data: dict, exc: Exception | None = None):
        self._data = data
        self._exc = exc

    def nodes(self, node: str) -> "_ChainableResource":
        return self

    def qemu(self, vmid: int) -> "_ChainableResource":
        return self

    def lxc(self, vmid: int) -> "_ChainableResource":
        return self

    @property
    def vncproxy(self) -> "_ChainableResource":
        return self

    @property
    def termproxy(self) -> "_ChainableResource":
        return self

    async def post(self, **kwargs) -> dict:
        if self._exc is not None:
            raise self._exc
        return self._data


class _FakePx:
    """Minimal fake ProxmoxSession returned by _open_session."""

    def __init__(self, data: dict, exc: Exception | None = None):
        self.session = _ChainableResource(data, exc)


def test_novnc_qemu_returns_200_with_ws_url(auth_test_client, db_engine):
    """noVNC + QEMU: returns 200 with a valid wss:// URL containing the ticket."""
    endpoint_id = _make_endpoint(db_engine)
    fake_px = _FakePx({"ticket": "ABCTICKET123", "port": 5900})

    with patch(
        "proxbox_api.routes.proxmox.console._open_session",
        new=AsyncMock(return_value=fake_px),
    ):
        resp = auth_test_client.post(
            "/proxmox/console/sessions",
            json={
                "endpoint_id": endpoint_id,
                "vmid": 100,
                "node": "pve01",
                "vm_type": "qemu",
                "console_type": "novnc",
            },
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ticket"] == "ABCTICKET123"
    assert data["port"] == 5900
    assert data["ws_url"].startswith("wss://")
    assert "ABCTICKET123" in data["ws_url"]
    assert "vncwebsocket" in data["ws_url"]
    assert data["proxmox_host"] == "10.0.0.1"
    assert data["proxmox_port"] == 8006
    assert data["console_type"] == "novnc"
    assert data["verify_ssl"] is False


def test_term_lxc_returns_200(auth_test_client, db_engine):
    """Terminal + LXC: returns 200 with a valid wss:// URL."""
    endpoint_id = _make_endpoint(db_engine)
    fake_px = _FakePx({"ticket": "TERMTICKET456", "port": 5901})

    with patch(
        "proxbox_api.routes.proxmox.console._open_session",
        new=AsyncMock(return_value=fake_px),
    ):
        resp = auth_test_client.post(
            "/proxmox/console/sessions",
            json={
                "endpoint_id": endpoint_id,
                "vmid": 200,
                "node": "pve02",
                "vm_type": "lxc",
                "console_type": "term",
            },
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ticket"] == "TERMTICKET456"
    assert data["port"] == 5901
    assert data["console_type"] == "term"
    assert data["ws_url"].startswith("wss://")
    assert "vncwebsocket" in data["ws_url"]


def test_missing_endpoint_returns_404(auth_test_client):
    """Unknown endpoint_id returns 404 before attempting a Proxmox connection."""
    resp = auth_test_client.post(
        "/proxmox/console/sessions",
        json={
            "endpoint_id": 99999,
            "vmid": 100,
            "node": "pve01",
            "vm_type": "qemu",
            "console_type": "novnc",
        },
    )

    assert resp.status_code == 404
    assert "99999" in resp.json()["detail"]


def test_session_open_failure_returns_502(auth_test_client, db_engine):
    """Connection failure when opening the Proxmox session returns 502."""
    endpoint_id = _make_endpoint(db_engine)

    with patch(
        "proxbox_api.routes.proxmox.console._open_session",
        new=AsyncMock(side_effect=ConnectionError("unreachable")),
    ):
        resp = auth_test_client.post(
            "/proxmox/console/sessions",
            json={
                "endpoint_id": endpoint_id,
                "vmid": 100,
                "node": "pve01",
                "vm_type": "qemu",
                "console_type": "novnc",
            },
        )

    assert resp.status_code == 502
    assert "Unable to connect" in resp.json()["detail"]


def test_proxmox_api_error_returns_502(auth_test_client, db_engine):
    """ProxmoxAPIError raised during the vncproxy/termproxy call returns 502."""
    endpoint_id = _make_endpoint(db_engine)
    fake_px = _FakePx({}, exc=ProxmoxAPIError("node offline"))

    with patch(
        "proxbox_api.routes.proxmox.console._open_session",
        new=AsyncMock(return_value=fake_px),
    ):
        resp = auth_test_client.post(
            "/proxmox/console/sessions",
            json={
                "endpoint_id": endpoint_id,
                "vmid": 100,
                "node": "pve01",
                "vm_type": "qemu",
                "console_type": "novnc",
            },
        )

    assert resp.status_code == 502
    assert "Proxmox console error" in resp.json()["detail"]


def test_lxc_novnc_returns_422(auth_test_client, db_engine):
    """LXC + novnc is rejected at the Pydantic validation layer with HTTP 422."""
    endpoint_id = _make_endpoint(db_engine)

    resp = auth_test_client.post(
        "/proxmox/console/sessions",
        json={
            "endpoint_id": endpoint_id,
            "vmid": 200,
            "node": "pve01",
            "vm_type": "lxc",
            "console_type": "novnc",
        },
    )

    assert resp.status_code == 422
    body = resp.json()
    detail_text = str(body)
    assert "novnc" in detail_text or "lxc" in detail_text


def test_vmid_zero_returns_422(auth_test_client, db_engine):
    """vmid=0 violates Field(ge=1) and must return HTTP 422."""
    endpoint_id = _make_endpoint(db_engine)

    resp = auth_test_client.post(
        "/proxmox/console/sessions",
        json={
            "endpoint_id": endpoint_id,
            "vmid": 0,
            "node": "pve01",
            "vm_type": "qemu",
            "console_type": "novnc",
        },
    )

    assert resp.status_code == 422


def test_empty_node_returns_422(auth_test_client, db_engine):
    """node="" violates Field(min_length=1) and must return HTTP 422."""
    endpoint_id = _make_endpoint(db_engine)

    resp = auth_test_client.post(
        "/proxmox/console/sessions",
        json={
            "endpoint_id": endpoint_id,
            "vmid": 100,
            "node": "",
            "vm_type": "qemu",
            "console_type": "novnc",
        },
    )

    assert resp.status_code == 422


def test_term_qemu_returns_200(auth_test_client, db_engine):
    """Terminal + QEMU: term console_type is valid for QEMU VMs."""
    endpoint_id = _make_endpoint(db_engine)
    fake_px = _FakePx({"ticket": "TERMQEMU789", "port": 5902})

    with patch(
        "proxbox_api.routes.proxmox.console._open_session",
        new=AsyncMock(return_value=fake_px),
    ):
        resp = auth_test_client.post(
            "/proxmox/console/sessions",
            json={
                "endpoint_id": endpoint_id,
                "vmid": 300,
                "node": "pve03",
                "vm_type": "qemu",
                "console_type": "term",
            },
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["console_type"] == "term"
    assert data["ticket"] == "TERMQEMU789"
    assert data["ws_url"].startswith("wss://")


def test_proxmox_data_envelope_unwrapped(auth_test_client, db_engine):
    """When Proxmox returns {"data": {...}}, the route unwraps the envelope."""
    endpoint_id = _make_endpoint(db_engine)
    # Proxmox-style envelope — the route must unwrap it
    fake_px = _FakePx({"data": {"ticket": "ENVELOPETICKET", "port": 5903}})

    with patch(
        "proxbox_api.routes.proxmox.console._open_session",
        new=AsyncMock(return_value=fake_px),
    ):
        resp = auth_test_client.post(
            "/proxmox/console/sessions",
            json={
                "endpoint_id": endpoint_id,
                "vmid": 400,
                "node": "pve04",
                "vm_type": "qemu",
                "console_type": "novnc",
            },
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ticket"] == "ENVELOPETICKET"
    assert data["port"] == 5903
