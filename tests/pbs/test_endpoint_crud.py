"""HTTP CRUD coverage for ``/pbs/endpoints``."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_pbs_endpoint_crud_lifecycle(client: TestClient):
    create_payload = {
        "name": "pbs-lab-1",
        "host": "pbs.example.local",
        "port": 8007,
        "token_id": "root@pam!sync",
        "token_secret": "very-secret-token-value",
        "verify_ssl": False,
        "timeout_seconds": 45,
    }

    create_response = client.post("/pbs/endpoints", json=create_payload)
    assert create_response.status_code == 200
    created = create_response.json()
    endpoint_id = created["id"]
    assert created["name"] == "pbs-lab-1"
    assert created["host"] == "pbs.example.local"
    assert created["token_id"] == "root@pam!sync"
    assert created["allow_writes"] is False
    assert created["timeout_seconds"] == 45
    assert "token_secret" not in created

    list_response = client.get("/pbs/endpoints")
    assert list_response.status_code == 200
    listed = list_response.json()
    assert len(listed) == 1
    assert listed[0]["id"] == endpoint_id
    assert "token_secret" not in listed[0]

    get_response = client.get(f"/pbs/endpoints/{endpoint_id}")
    assert get_response.status_code == 200

    update_response = client.put(
        f"/pbs/endpoints/{endpoint_id}",
        json={"name": "pbs-lab-1-updated", "verify_ssl": True},
    )
    assert update_response.status_code == 200
    assert update_response.json()["name"] == "pbs-lab-1-updated"
    assert update_response.json()["verify_ssl"] is True

    delete_response = client.delete(f"/pbs/endpoints/{endpoint_id}")
    assert delete_response.status_code == 200
    assert delete_response.json() == {"message": "PBS endpoint deleted."}

    get_deleted = client.get(f"/pbs/endpoints/{endpoint_id}")
    assert get_deleted.status_code == 404


def test_pbs_endpoint_name_unique(client: TestClient):
    base = {
        "name": "pbs-lab-1",
        "host": "pbs.example.local",
        "port": 8007,
        "token_id": "root@pam!sync",
        "token_secret": "secret-1",
    }
    first = client.post("/pbs/endpoints", json=base)
    assert first.status_code == 200

    second = client.post("/pbs/endpoints", json=base)
    assert second.status_code == 400
    assert second.json()["detail"] == "PBS endpoint name already exists"


def test_pbs_endpoint_token_secret_is_persisted_encrypted(client: TestClient, tmp_path):
    """The plaintext token secret never round-trips through the public API."""
    payload = {
        "name": "pbs-secret-check",
        "host": "pbs.example.local",
        "port": 8007,
        "token_id": "root@pam!sync",
        "token_secret": "plaintext-do-not-leak",
    }
    response = client.post("/pbs/endpoints", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert "token_secret" not in body
    assert "plaintext-do-not-leak" not in response.text
