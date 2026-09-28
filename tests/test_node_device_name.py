"""Node Device name template validation and rendering tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.services.sync import node_device_name
from proxbox_api.services.sync.devices import (
    _discover_device_items,
    _report_device_name_errors,
    create_proxmox_devices,
)
from proxbox_api.session.proxmox_providers import _parse_db_endpoint


@pytest.mark.parametrize(
    "template",
    (
        "{node}.{unknown}",
        "{cluster}.example.com",
        "{node:>10}",
        "{node!r}",
        "{node.name}",
        "{node[0]}",
        "{node}_bad",
        "{node}." + ("a" * 64),
    ),
)
def test_template_validator_rejects_unsafe_templates(template: str) -> None:
    with pytest.raises(ValueError):
        node_device_name.validate_node_device_name_template(template)


def test_render_node_device_name_supports_all_placeholders() -> None:
    assert (
        node_device_name.render_node_device_name(
            "prox01",
            "Cluster A",
            "endpoint-a",
            "{node}.{cluster_slug}.{endpoint}",
        )
        == "prox01.cluster-a.endpoint-a"
    )


def test_render_rejects_node_specific_overlong_name() -> None:
    with pytest.raises(node_device_name.NodeDeviceNameError) as raised:
        node_device_name.render_node_device_name(
            "prox01",
            "a" * 63,
            "endpoint-a",
            "{node}.{cluster}",
        )

    assert raised.value.node == "prox01"
    assert raised.value.cluster == "a" * 63
    assert raised.value.rendered_length == 70


def test_discovery_reports_bad_node_and_keeps_other_nodes() -> None:
    clusters = [
        SimpleNamespace(
            name="a" * 63,
            endpoint_name="endpoint-a",
            node_device_name_template="{node}.{cluster}",
            node_list=[SimpleNamespace(name="pve01")],
        ),
        SimpleNamespace(
            name="lab",
            endpoint_name="endpoint-a",
            node_device_name_template="{node}.{cluster}",
            node_list=[SimpleNamespace(name="pve02")],
        ),
    ]
    events: list[dict[str, object]] = []

    class Bridge:
        async def emit_item_progress(self, **payload: object) -> None:
            events.append(payload)

    items, _clusters, errors = _discover_device_items(clusters)
    asyncio.run(_report_device_name_errors(errors, Bridge()))  # type: ignore[arg-type]

    assert [item["name"] for item in items] == ["pve02.lab"]
    assert len(errors) == 1
    assert events[0]["status"] == "failed"
    assert "length 69" in str(events[0]["error"])


def test_bulk_device_finalization_keeps_duplicate_cluster_node_endpoints_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Record:
        def __init__(self, record_id: int) -> None:
            self.record_id = record_id

        def serialize(self) -> dict[str, object]:
            return {"id": self.record_id, "name": f"device-{self.record_id}"}

    clusters = [
        SimpleNamespace(
            name="shared-cluster",
            mode="cluster",
            db_endpoint_id=endpoint_id,
            endpoint_name=f"endpoint-{endpoint_id}",
            node_device_name_template="{node}.{endpoint}",
            node_list=[SimpleNamespace(name="pve01")],
        )
        for endpoint_id in (501, 502)
    ]

    async def _reconcile(*_args, **_kwargs):
        return {
            (501, "shared-cluster", "pve01"): Record(1501),
            (502, "shared-cluster", "pve01"): Record(1502),
        }

    async def _hardware(*_args, **_kwargs):
        return None

    monkeypatch.setattr("proxbox_api.services.sync.devices._reconcile_devices_or_raise", _reconcile)
    monkeypatch.setattr("proxbox_api.services.sync.devices._run_hardware_discovery", _hardware)

    result = asyncio.run(
        create_proxmox_devices(
            netbox_session=object(),
            clusters_status=clusters,
            tag=SimpleNamespace(name="Proxbox", slug="proxbox", color="ff5722"),
        )
    )

    assert [record["id"] for record in result] == [1501, 1502]


def test_endpoint_template_precedes_global_and_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROXBOX_NODE_DEVICE_NAME_TEMPLATE", "{node}.environment")

    assert (
        node_device_name.resolve_node_device_name_template(
            "{node}.endpoint",
            global_template="{node}.global",
        )
        == "{node}.endpoint"
    )
    assert (
        node_device_name.resolve_node_device_name_template(
            "",
            global_template="{node}.global",
        )
        == "{node}.environment"
    )


def test_invalid_effective_template_falls_back_to_short_name_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged: list[tuple[object, ...]] = []
    monkeypatch.setenv("PROXBOX_NODE_DEVICE_NAME_TEMPLATE", "{cluster}")
    monkeypatch.setattr(node_device_name.logger, "error", lambda *args: logged.append(args))

    assert (
        node_device_name.resolve_node_device_name_template("", endpoint_name="endpoint-a")
        == "{node}"
    )
    assert logged[0][1:3] == ("{cluster}", "endpoint-a")


def test_database_endpoint_template_precedes_global_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        node_device_name,
        "get_str",
        lambda *, settings_key, env, default: default,
    )
    endpoint = ProxmoxEndpoint(
        name="endpoint-a",
        ip_address="192.0.2.10",
        username="root@pam",
        node_device_name_template="",
    )

    inherited = _parse_db_endpoint(
        endpoint,
        {"node_device_name_template": "{node}.{cluster_slug}"},
    )
    endpoint.node_device_name_template = "{node}.{endpoint}"
    overridden = _parse_db_endpoint(
        endpoint,
        {"node_device_name_template": "{node}.{cluster_slug}"},
    )

    assert inherited.node_device_name_template == "{node}.{cluster_slug}"
    assert overridden.node_device_name_template == "{node}.{endpoint}"
