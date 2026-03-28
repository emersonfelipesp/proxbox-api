from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from proxbox_api.database import NetBoxEndpoint
from proxbox_api.exception import ProxboxException
from proxbox_api.main import standalone_info
from proxbox_api.routes.netbox import (
    create_netbox_endpoint,
    delete_netbox_endpoint,
    get_netbox_endpoint,
    get_netbox_endpoints,
    netbox_openapi,
    netbox_status,
    update_netbox_endpoint,
)
from proxbox_api.routes.proxmox.endpoints import (
    ProxmoxEndpointCreate,
    ProxmoxEndpointUpdate,
    create_proxmox_endpoint,
    delete_proxmox_endpoint,
    get_proxmox_endpoint,
    get_proxmox_endpoints,
    update_proxmox_endpoint,
)


def test_root_route_returns_service_metadata():
    body = asyncio.run(standalone_info())
    assert body["message"] == "Proxbox Backend made in FastAPI framework"
    assert body["proxbox"]["github"].endswith("netbox-proxbox")


def test_proxmox_endpoint_crud_lifecycle(db_session):
    created = create_proxmox_endpoint(
        ProxmoxEndpointCreate(
            name="pve-lab-1",
            ip_address="10.0.0.10",
            domain="pve-lab-1.local",
            port=8006,
            username="root@pam",
            password="supersecret",
            verify_ssl=False,
        ),
        db_session,
    )
    endpoint_id = created.id
    assert created.name == "pve-lab-1"
    assert created.password == "supersecret"

    listed = get_proxmox_endpoints(db_session)
    assert len(listed) == 1

    updated = update_proxmox_endpoint(
        endpoint_id,
        ProxmoxEndpointUpdate(
            name="pve-lab-1-updated",
            verify_ssl=True,
            token_name="sync",
            token_value="secret-token",
            password=None,
        ),
        db_session,
    )
    assert updated.name == "pve-lab-1-updated"
    assert updated.token_name == "sync"
    assert updated.verify_ssl is True

    deleted = delete_proxmox_endpoint(endpoint_id, db_session)
    assert deleted == {"message": "Proxmox endpoint deleted."}

    with pytest.raises(HTTPException, match="Proxmox endpoint not found"):
        get_proxmox_endpoint(endpoint_id, db_session)


def test_proxmox_endpoint_requires_complete_token_pair(db_session):
    with pytest.raises(
        HTTPException,
        match="token_name and token_value must be provided together",
    ):
        create_proxmox_endpoint(
            ProxmoxEndpointCreate(
                name="pve-lab-2",
                ip_address="10.0.0.11",
                port=8006,
                username="root@pam",
                token_name="sync",
                verify_ssl=True,
            ),
            db_session,
        )


def test_netbox_endpoint_crud_and_singleton_rule(db_session):
    payload = NetBoxEndpoint(
        name="netbox-primary",
        ip_address="10.0.0.20",
        domain="netbox.local",
        port=443,
        token="token-1",
        verify_ssl=True,
    )
    created = create_netbox_endpoint(payload, db_session)
    endpoint_id = created.id

    with pytest.raises(HTTPException, match="Only one NetBox endpoint is allowed"):
        create_netbox_endpoint(
            NetBoxEndpoint(
                name="netbox-secondary",
                ip_address="10.0.0.21",
                domain="netbox2.local",
                port=443,
                token="token-2",
                verify_ssl=True,
            ),
            db_session,
        )

    listed = get_netbox_endpoints(db_session)
    assert len(listed) == 1

    updated = update_netbox_endpoint(
        endpoint_id,
        NetBoxEndpoint(
            name="netbox-primary-updated",
            ip_address="10.0.0.20",
            domain="netbox.local",
            port=443,
            token="token-2",
            verify_ssl=True,
        ),
        db_session,
    )
    assert updated.name == "netbox-primary-updated"

    assert get_netbox_endpoint(endpoint_id, db_session).token == "token-2"
    assert delete_netbox_endpoint(endpoint_id, db_session) == {
        "message": "NetBox Endpoint deleted."
    }


def test_netbox_endpoint_rejects_v1_without_token(db_session):
    with pytest.raises(HTTPException, match="token is required for NetBox API token v1"):
        create_netbox_endpoint(
            NetBoxEndpoint(
                name="netbox-primary",
                ip_address="10.0.0.20",
                domain="netbox.local",
                port=443,
                token_version="v1",
                token="",
                verify_ssl=True,
            ),
            db_session,
        )


def test_netbox_endpoint_rejects_v2_incomplete_token(db_session):
    with pytest.raises(
        HTTPException,
        match="token_key and token \\(secret\\) must both be set",
    ):
        create_netbox_endpoint(
            NetBoxEndpoint(
                name="netbox-primary",
                ip_address="10.0.0.20",
                domain="netbox.local",
                port=443,
                token_version="v2",
                token_key="myid",
                token="",
                verify_ssl=True,
            ),
            db_session,
        )


def test_netbox_endpoint_accepts_v2_token(db_session):
    created = create_netbox_endpoint(
        NetBoxEndpoint(
            name="netbox-v2",
            ip_address="10.0.0.20",
            domain="netbox.local",
            port=443,
            token_version="v2",
            token_key="myid",
            token="secretpart",
            verify_ssl=True,
        ),
        db_session,
    )
    assert created.token_version == "v2"
    assert created.token_key == "myid"
    assert created.token == "secretpart"


def test_netbox_status_and_openapi_routes_are_mocked(client_with_fake_netbox):
    fake_session = client_with_fake_netbox

    status_body = asyncio.run(netbox_status(fake_session))
    assert status_body["status"] == "ok"

    openapi_body = asyncio.run(netbox_openapi(fake_session))
    assert "/api/virtualization/virtual-machines/" in openapi_body["paths"]


def test_netbox_status_route_wraps_dependency_errors():
    class BrokenNetBoxSession:
        def status(self):
            raise RuntimeError("boom")

    with pytest.raises(ProxboxException, match="Error fetching status from NetBox API."):
        asyncio.run(netbox_status(BrokenNetBoxSession()))
