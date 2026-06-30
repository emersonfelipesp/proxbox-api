from __future__ import annotations

from fastapi.testclient import TestClient


def test_proxmox_mock_package_is_importable() -> None:
    import proxmox_mock.main

    assert proxmox_mock.main.app is not None


def test_proxmox_mock_root_reports_configured_service(monkeypatch) -> None:
    from proxmox_mock.app import create_mock_app

    monkeypatch.setenv("PROXMOX_MOCK_SERVICE", "pbs")

    response = TestClient(create_mock_app()).get("/")

    assert response.status_code == 200
    assert response.json()["service"] == "pbs"
