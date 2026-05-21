"""Tests for Issue #376 sub-PR B: operational verbs gate stub.

Pins the load-bearing trust boundary (``operational-verbs.md`` §2.3
layer 3): every operational-verb route returns 403 with a structured
``reason`` until ``ProxmoxEndpoint.allow_writes`` is True on the
target endpoint.

After sub-PR B lands, the only path that exits the 403 branch is
``_not_implemented`` returning 501. Sub-PRs C–F replace that with the
real dispatch. The 403 gate at the top of every route must remain.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session

from proxbox_api.database import ProxmoxEndpoint

VERB_PATHS = [
    ("/proxmox/qemu/100/start"),
    ("/proxmox/lxc/100/start"),
    ("/proxmox/qemu/100/stop"),
    ("/proxmox/lxc/100/stop"),
    ("/proxmox/qemu/100/snapshot"),
    ("/proxmox/lxc/100/snapshot"),
    ("/proxmox/qemu/100/migrate"),
    ("/proxmox/lxc/100/migrate"),
]

# All four verbs were wired by sub-PRs C–F; no paths remain on the sub-PR
# B 501 stub. The fallthrough-to-501 test that used STUB_VERB_PATHS is
# removed because there is nothing left to fall through.


def _make_endpoint(db_engine, allow_writes: bool) -> int:
    """Insert a ProxmoxEndpoint into the test DB and return its id."""
    with Session(db_engine) as session:
        endpoint = ProxmoxEndpoint(
            name="pve-test",
            ip_address="10.0.0.10",
            port=8006,
            username="root@pam",
            verify_ssl=False,
            allow_writes=allow_writes,
        )
        session.add(endpoint)
        session.commit()
        session.refresh(endpoint)
        assert endpoint.id is not None
        return endpoint.id


@pytest.mark.parametrize("path", VERB_PATHS)
def test_missing_endpoint_id_returns_403(auth_test_client, path: str):
    resp = auth_test_client.post(path)
    assert resp.status_code == 403
    body = resp.json()
    assert body["reason"] == "endpoint_id_required"


@pytest.mark.parametrize("path", VERB_PATHS)
def test_unknown_endpoint_id_returns_403(auth_test_client, path: str):
    resp = auth_test_client.post(path, params={"endpoint_id": 999})
    assert resp.status_code == 403
    body = resp.json()
    assert body["reason"] == "endpoint_not_found"


@pytest.mark.parametrize("path", VERB_PATHS)
def test_endpoint_with_writes_disabled_returns_403(auth_test_client, db_engine, path: str):
    endpoint_id = _make_endpoint(db_engine, allow_writes=False)
    resp = auth_test_client.post(path, params={"endpoint_id": endpoint_id})
    assert resp.status_code == 403
    body = resp.json()
    assert body["reason"] == "endpoint_writes_disabled"
    assert body["endpoint_id"] == endpoint_id


def test_allow_writes_field_defaults_to_false():
    """The SQLModel default for allow_writes is False (gate closed by default)."""
    assert ProxmoxEndpoint.model_fields["allow_writes"].default is False
