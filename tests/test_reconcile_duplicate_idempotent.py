from __future__ import annotations

import json

import pytest
from netbox_sdk.client import ApiResponse

from proxbox_api import netbox_rest
from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import (
    clear_rest_get_cache,
    rest_bulk_reconcile_async,
    rest_reconcile_async_with_status,
)


class _RestClient:
    def __init__(self, *, post_status: int, post_body: object):
        self.post_status = post_status
        self.post_body = post_body
        self.calls: list[tuple[str, str, dict[str, object] | None, object]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, object] | None = None,
        payload: object = None,
        expect_json: bool = True,
    ) -> ApiResponse:
        del expect_json
        self.calls.append((method, path, query, payload))
        if method == "GET":
            body: object = {"count": 0, "results": []}
            status = 200
        elif method == "POST":
            body = self.post_body
            status = self.post_status
        else:  # pragma: no cover - these tests only exercise GET and POST
            body = {"detail": f"unexpected {method}"}
            status = 500
        text = body if isinstance(body, str) else json.dumps(body)
        return ApiResponse(status=status, text=text, headers={"Content-Type": "application/json"})


class _Session:
    def __init__(self, *, post_status: int, post_body: object):
        self.client = _RestClient(post_status=post_status, post_body=post_body)


def _interface_payload() -> dict[str, object]:
    return {"name": "net0", "virtual_machine": 8, "enabled": True}


def _normalizer(record: dict[str, object]) -> dict[str, object]:
    return {
        "name": record.get("name"),
        "virtual_machine": record.get("virtual_machine"),
        "enabled": record.get("enabled"),
    }


@pytest.fixture(autouse=True)
def _clear_rest_cache():
    clear_rest_get_cache()
    yield
    clear_rest_get_cache()


@pytest.mark.asyncio
async def test_confirmed_duplicate_without_refetch_returns_unchanged(monkeypatch):
    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(netbox_rest.asyncio, "sleep", _no_sleep)
    session = _Session(
        post_status=400,
        post_body={
            "non_field_errors": ["Interface with this Virtual machine and Name already exists."]
        },
    )

    result = await rest_reconcile_async_with_status(
        session,
        "/api/virtualization/interfaces/",
        lookup={"name": "net0", "virtual_machine": 8},
        payload=_interface_payload(),
        schema=dict,
        current_normalizer=_normalizer,
        strict_lookup=True,
        lookup_query_field_map={"virtual_machine": "virtual_machine_id"},
    )

    assert result.status == "unchanged"
    assert result.record.get("name") == "net0"
    assert result.record.id is None


@pytest.mark.asyncio
async def test_bulk_reconcile_counts_confirmed_duplicate_miss_as_unchanged(monkeypatch):
    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(netbox_rest.asyncio, "sleep", _no_sleep)
    session = _Session(
        post_status=400,
        post_body={
            "non_field_errors": ["Interface with this Virtual machine and Name already exists."]
        },
    )

    result = await rest_bulk_reconcile_async(
        session,
        "/api/virtualization/interfaces/",
        payloads=[_interface_payload()],
        lookup_fields=["name", "virtual_machine"],
        schema=dict,
        current_normalizer=_normalizer,
        strict_lookup=True,
        lookup_query_field_map={"virtual_machine": "virtual_machine_id"},
    )

    assert result.created == 0
    assert result.updated == 0
    assert result.unchanged == 1
    assert result.failed == 0


@pytest.mark.asyncio
async def test_non_duplicate_create_error_still_raises_and_counts_failed(monkeypatch):
    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(netbox_rest.asyncio, "sleep", _no_sleep)
    session = _Session(post_status=400, post_body={"detail": "field is invalid"})

    with pytest.raises(ProxboxException) as excinfo:
        await rest_reconcile_async_with_status(
            session,
            "/api/virtualization/interfaces/",
            lookup={"name": "net0", "virtual_machine": 8},
            payload=_interface_payload(),
            schema=dict,
            current_normalizer=_normalizer,
            strict_lookup=True,
            lookup_query_field_map={"virtual_machine": "virtual_machine_id"},
        )

    assert "field is invalid" in str(excinfo.value)

    bulk_result = await rest_bulk_reconcile_async(
        session,
        "/api/virtualization/interfaces/",
        payloads=[_interface_payload()],
        lookup_fields=["name", "virtual_machine"],
        schema=dict,
        current_normalizer=_normalizer,
        strict_lookup=True,
        lookup_query_field_map={"virtual_machine": "virtual_machine_id"},
    )

    assert bulk_result.created == 0
    assert bulk_result.updated == 0
    assert bulk_result.unchanged == 0
    assert bulk_result.failed == 1
