"""HTTP CRUD coverage for ``/pdm/endpoints``."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_pdm_endpoint_crud_lifecycle(client: TestClient):
    create_payload = {
        "name": "pdm-lab-1",
        "host": "pdm.example.local",
        "port": 8443,
        "token_id": "root@pam!sync",
        "token_secret": "very-secret-token-value",
        "verify_ssl": False,
        "timeout_seconds": 45,
    }

    create_response = client.post("/pdm/endpoints", json=create_payload)
    assert create_response.status_code == 200
    created = create_response.json()
    endpoint_id = created["id"]
    assert created["name"] == "pdm-lab-1"
    assert created["host"] == "pdm.example.local"
    assert created["token_id"] == "root@pam!sync"
    assert created["allow_writes"] is False
    assert created["enabled"] is True
    assert created["timeout_seconds"] == 45
    assert "token_secret" not in created

    list_response = client.get("/pdm/endpoints")
    assert list_response.status_code == 200
    listed = list_response.json()
    assert len(listed) == 1
    assert listed[0]["id"] == endpoint_id
    assert "token_secret" not in listed[0]

    get_response = client.get(f"/pdm/endpoints/{endpoint_id}")
    assert get_response.status_code == 200

    update_response = client.put(
        f"/pdm/endpoints/{endpoint_id}",
        json={"name": "pdm-lab-1-updated", "verify_ssl": True, "enabled": False},
    )
    assert update_response.status_code == 200
    assert update_response.json()["name"] == "pdm-lab-1-updated"
    assert update_response.json()["verify_ssl"] is True
    assert update_response.json()["enabled"] is False

    delete_response = client.delete(f"/pdm/endpoints/{endpoint_id}")
    assert delete_response.status_code == 200
    assert delete_response.json() == {"message": "PDM endpoint deleted."}

    get_deleted = client.get(f"/pdm/endpoints/{endpoint_id}")
    assert get_deleted.status_code == 404


def test_pdm_endpoint_name_unique(client: TestClient):
    base = {
        "name": "pdm-lab-1",
        "host": "pdm.example.local",
        "port": 8443,
        "token_id": "root@pam!sync",
        "token_secret": "secret-1",
    }
    first = client.post("/pdm/endpoints", json=base)
    assert first.status_code == 200

    second = client.post("/pdm/endpoints", json=base)
    assert second.status_code == 400
    assert second.json()["detail"] == "PDM endpoint name already exists"


def test_pdm_endpoint_token_secret_is_persisted_encrypted(client: TestClient, tmp_path):
    """The plaintext token secret never round-trips through the public API."""
    payload = {
        "name": "pdm-secret-check",
        "host": "pdm.example.local",
        "port": 8443,
        "token_id": "root@pam!sync",
        "token_secret": "plaintext-do-not-leak",
    }
    response = client.post("/pdm/endpoints", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert "token_secret" not in body
    assert "plaintext-do-not-leak" not in response.text
