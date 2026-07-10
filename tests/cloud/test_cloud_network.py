"""Tests for managed cloud customer-network allocation helpers."""

from __future__ import annotations

import asyncio
import json

import pytest
from netbox_sdk.client import ApiResponse

from proxbox_api.services import cloud_network


class _RestClient:
    def __init__(self, responses: dict[tuple[str, str], tuple[int, object]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, dict[str, object] | None, object, bool]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, object] | None = None,
        payload: object = None,
        expect_json: bool = True,
    ) -> ApiResponse:
        self.calls.append((method, path, query, payload, expect_json))
        status, body = self._responses[(method, path)]
        text = body if isinstance(body, str) else json.dumps(body)
        return ApiResponse(status=status, text=text, headers={"Content-Type": "application/json"})


class _NetBox:
    def __init__(self, responses: dict[tuple[str, str], tuple[int, object]]) -> None:
        self.client = _RestClient(responses)


def test_resolve_cloud_network_reads_runtime_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_get_bool(**kwargs: object) -> bool:
        assert kwargs["settings_key"] == "cloud_network_lock_enabled"
        return True

    def _fake_get_int(**kwargs: object) -> int:
        key = kwargs["settings_key"]
        if key == "cloud_customer_prefix_id":
            return 123
        if key == "cloud_customer_vlan_tag":
            return 2050
        raise AssertionError(f"unexpected key {key}")

    def _fake_get_str(**kwargs: object) -> str:
        key = kwargs["settings_key"]
        if key == "cloud_customer_bridge":
            return "vmbr1"
        if key == "cloud_customer_gateway":
            return "168.0.98.1"
        raise AssertionError(f"unexpected key {key}")

    monkeypatch.setattr(cloud_network.runtime_settings, "get_bool", _fake_get_bool)
    monkeypatch.setattr(cloud_network.runtime_settings, "get_int", _fake_get_int)
    monkeypatch.setattr(cloud_network.runtime_settings, "get_str", _fake_get_str)

    resolved = cloud_network.resolve_cloud_network()

    assert resolved == cloud_network.CloudNetworkConfig(
        lock_enabled=True,
        prefix_id=123,
        bridge="vmbr1",
        vlan_tag=2050,
        gateway="168.0.98.1",
    )


def test_peek_available_ips_lists_without_occupying() -> None:
    nb = _NetBox(
        {
            ("GET", "/api/ipam/prefixes/123/available-ips/"): (
                200,
                {
                    "count": 2,
                    "results": [
                        {"address": "168.0.98.10/24"},
                        {"address": "168.0.98.11/24"},
                    ],
                },
            )
        }
    )

    result = asyncio.run(cloud_network.peek_available_ips(123, 2, netbox_session=nb))

    assert result == [
        cloud_network.AvailableIPAddress(address="168.0.98.10/24"),
        cloud_network.AvailableIPAddress(address="168.0.98.11/24"),
    ]
    assert nb.client.calls == [
        ("GET", "/api/ipam/prefixes/123/available-ips/", {"limit": 2}, None, True)
    ]


def test_allocate_ip_posts_status_and_optional_vminterface_binding() -> None:
    nb = _NetBox(
        {
            ("POST", "/api/ipam/prefixes/123/available-ips/"): (
                201,
                {"id": 77, "address": "168.0.98.10/24"},
            )
        }
    )

    result = asyncio.run(
        cloud_network.allocate_ip(
            123,
            vminterface_id=55,
            netbox_session=nb,
        )
    )

    assert result == cloud_network.AllocatedIPAddress(
        id=77,
        address="168.0.98.10",
        cidr="168.0.98.10/24",
    )
    assert nb.client.calls == [
        (
            "POST",
            "/api/ipam/prefixes/123/available-ips/",
            None,
            {
                "status": "active",
                "assigned_object_type": "virtualization.vminterface",
                "assigned_object_id": 55,
            },
            True,
        )
    ]
