"""Focused contracts for bounded Proxmox metrics pull transport."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from proxbox_api.schemas.proxmox_metrics import ProxmoxMetricsPullRequest
from proxbox_api.services import proxmox_metrics
from proxbox_api.services.proxmox_metrics import (
    ProxmoxMetricsPullError,
    execute_proxmox_metrics_pull,
    normalize_proxmox_metrics,
)
from proxbox_api.session import proxmox_providers


def _request(**overrides: object) -> ProxmoxMetricsPullRequest:
    values: dict[str, object] = {"endpoint_id": 17}
    values.update(overrides)
    return ProxmoxMetricsPullRequest.model_validate(values)


def _row(
    object_id: str = "qemu/101",
    metric: str = "cpu_current",
    timestamp: int = 1_700_000_001,
    value: float = 0.5,
    metric_type: str = "gauge",
) -> dict[str, object]:
    return {
        "id": object_id,
        "metric": metric,
        "timestamp": timestamp,
        "value": value,
        "type": metric_type,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"history": True},
        {"start_time": 4_102_444_801},
        {"target_name": "pve-a", "target_domain": "pve.example.test"},
        {"endpoint_id": 17, "target_name": "pve-a"},
        {"object_ids": ["../../nodes"]},
        {"metric_names": ["cpu\nsecret"]},
        {"max_rows": 5001},
    ],
)
def test_pull_request_rejects_unbounded_or_ambiguous_input(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _request(**overrides)


def test_normalization_filters_sorts_deduplicates_and_truncates() -> None:
    captured = datetime(2026, 9, 9, 21, 0, tzinfo=UTC)
    payload = {
        "data": [
            _row("node/pve-b", "mem_used", 1_700_000_003, 30),
            _row("qemu/101", "cpu_current", 1_700_000_002, 20),
            _row("qemu/101", "cpu_current", 1_700_000_002, 20),
            _row("qemu/101", "mem_used", 1_700_000_001, 10),
            _row("lxc/202", "cpu_current", 1_700_000_004, 40),
        ]
    }

    result = normalize_proxmox_metrics(
        payload,
        _request(
            start_time=1_700_000_000,
            object_prefixes=["qemu"],
            metric_names=["cpu_current", "mem_used"],
            max_rows=1,
        ),
        captured_at=captured,
    )

    assert result.row_count == 1
    assert result.truncated is True
    assert result.deduplicated_count == 1
    assert result.rows == [
        {
            "object_id": "qemu/101",
            "metric": "mem_used",
            "timestamp": 1_700_000_001,
            "value": 10.0,
            "metric_type": "gauge",
            "source": "pull",
        }
    ]
    assert result.columns == [
        "object_id",
        "metric",
        "timestamp",
        "value",
        "metric_type",
        "source",
    ]
    assert result.response_format == "proxmox_export"


@pytest.mark.parametrize(
    "payload",
    [
        {"data": "not-a-list"},
        {"data": ["not-an-object"]},
        {"data": [_row(value=float("nan"))]},
        {"data": [_row(timestamp=4_102_444_801)]},
        {
            "data": [
                _row(value=1),
                _row(value=2),
            ]
        },
    ],
)
def test_normalization_rejects_hostile_or_conflicting_provider_rows(
    payload: object,
) -> None:
    with pytest.raises(ProxmoxMetricsPullError) as caught:
        normalize_proxmox_metrics(payload, _request())

    assert caught.value.reason == "pull_invalid_response"


def test_normalization_enforces_provider_response_byte_bound() -> None:
    with pytest.raises(ProxmoxMetricsPullError) as caught:
        normalize_proxmox_metrics(
            {
                "data": [
                    _row(
                        object_id=f"qemu/{index}-" + "x" * 230,
                        timestamp=1_700_000_001 + index,
                    )
                    for index in range(6)
                ]
            },
            _request(max_response_bytes=1024),
        )

    assert caught.value.reason == "pull_response_too_large"


async def test_execute_uses_fixed_export_path_parameters_and_closes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class Resource:
        async def get_bounded(
            self, maximum_bytes: int, **parameters: object
        ) -> list[dict[str, object]]:
            observed["maximum_bytes"] = maximum_bytes
            observed["parameters"] = parameters
            return [_row()]

    class Target:
        def session(self, path: str) -> Resource:
            observed["path"] = path
            return Resource()

        async def aclose(self) -> None:
            observed["closed"] = True

    async def resolve(**kwargs: object) -> Target:
        observed["selectors"] = kwargs
        return Target()

    monkeypatch.setattr(proxmox_metrics, "resolve_proxmox_target_session", resolve)
    request = _request(
        source="netbox",
        start_time=1_700_000_000,
        history=True,
        local_only=True,
    )

    result = await execute_proxmox_metrics_pull(request, object())  # type: ignore[arg-type]

    assert observed["path"] == "cluster/metrics/export"
    assert observed["maximum_bytes"] == request.max_response_bytes
    assert observed["parameters"] == {
        "history": True,
        "local-only": True,
        "start-time": 1_700_000_000,
    }
    assert observed["closed"] is True
    selectors = observed["selectors"]
    assert isinstance(selectors, dict)
    assert selectors["source"] == "netbox"
    assert selectors["endpoint_id"] == 17
    assert result.rows[0]["source"] == "pull"


async def test_execute_maps_provider_exception_without_leaking_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(**_kwargs: object) -> object:
        raise RuntimeError("Authorization: secret-provider-token")

    monkeypatch.setattr(proxmox_metrics, "resolve_proxmox_target_session", fail)

    with pytest.raises(ProxmoxMetricsPullError) as caught:
        await execute_proxmox_metrics_pull(_request(), object())  # type: ignore[arg-type]

    assert caught.value.reason == "pull_unavailable"
    assert str(caught.value) == "pull_unavailable"
    assert "secret-provider-token" not in str(caught.value)


async def test_exact_endpoint_id_is_forwarded_to_the_bounded_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    schema = SimpleNamespace(name="pve-a", domain="pve-a.test", ip_address="10.0.0.1")
    expected_session = object()

    async def load(**kwargs: object) -> list[SimpleNamespace]:
        observed.update(kwargs)
        return [schema]

    async def create(received: object) -> object:
        assert received is schema
        return expected_session

    monkeypatch.setattr(proxmox_providers, "load_proxmox_session_schemas", load)
    monkeypatch.setattr(proxmox_providers.ProxmoxSession, "create", create)

    result = await proxmox_providers.resolve_proxmox_target_session(
        object(),
        source="netbox",
        endpoint_id=17,  # type: ignore[arg-type]
    )

    assert result is expected_session
    assert observed["source"] == "netbox"
    assert observed["endpoint_ids"] == [17]


async def test_pull_route_is_authenticated_and_registered(test_client) -> None:
    response = test_client.post("/proxmox/metrics/pull/query", json={})

    assert response.status_code == 401
