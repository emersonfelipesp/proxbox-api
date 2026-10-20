"""Endpoint-exact atomic native-cluster ownership and reconciliation contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from proxbox_api.netbox_rest import RestRecord
from proxbox_api.services import netbox_writers
from proxbox_api.services.sync import cluster_links, device_ensure


def _source(endpoint_id=73, *, endpoint_source="database", host="192.0.2.10", **extra):
    return SimpleNamespace(
        db_endpoint_id=endpoint_id,
        endpoint_source=endpoint_source,
        ip_address=host,
        domain=None,
        http_port=8006,
        **extra,
    )


def _endpoint(endpoint_id=5, host="192.0.2.10", **extra):
    return {
        "id": endpoint_id,
        "ip_address": {"address": f"{host}/32"},
        "port": 8006,
        "enabled": True,
        **extra,
    }


def _endpoint_rows(monkeypatch, rows):
    async def read(_nb, path):
        assert path == cluster_links.ENDPOINT_PATH
        return rows

    monkeypatch.setattr(cluster_links, "rest_list_async", read)


@pytest.mark.parametrize("source_kind", ["database", None])
async def test_backend_primary_key_is_never_treated_as_plugin_primary_key(monkeypatch, source_kind):
    _endpoint_rows(monkeypatch, [_endpoint(73, "192.0.2.99"), _endpoint(5)])
    assert (
        await cluster_links.resolve_plugin_endpoint_id(
            object(), _source(endpoint_source=source_kind)
        )
        == 5
    )


async def test_netbox_source_requires_exact_primary_key_and_transport(monkeypatch):
    _endpoint_rows(monkeypatch, [_endpoint(5), _endpoint(73, "192.0.2.99")])
    with pytest.raises(cluster_links.ClusterBindingError):
        await cluster_links.resolve_plugin_endpoint_id(object(), _source(endpoint_source="netbox"))
    assert (
        await cluster_links.resolve_plugin_endpoint_id(
            object(), _source(5, endpoint_source="netbox")
        )
        == 5
    )


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [_endpoint(5), _endpoint(6)],
        [_endpoint(5, "192.0.2.99")],
        [_endpoint(5, port=8443)],
        [_endpoint(5, enabled=False)],
    ],
)
async def test_unresolved_ambiguous_disabled_or_mismatched_transport_is_refused(monkeypatch, rows):
    _endpoint_rows(monkeypatch, rows)
    with pytest.raises(cluster_links.ClusterBindingError):
        await cluster_links.resolve_plugin_endpoint_id(object(), _source())


async def test_conflicting_domain_refuses_even_matching_ip(monkeypatch):
    _endpoint_rows(monkeypatch, [_endpoint(5, domain="other.example.test")])
    source = _source()
    source.domain = "expected.example.test"
    with pytest.raises(cluster_links.ClusterBindingError):
        await cluster_links.resolve_plugin_endpoint_id(object(), source)


async def test_name_and_singleton_are_never_identity_evidence(monkeypatch):
    _endpoint_rows(monkeypatch, [{"id": 5, "name": "lab", "enabled": True}])
    with pytest.raises(cluster_links.ClusterBindingError):
        await cluster_links.resolve_plugin_endpoint_id(object(), SimpleNamespace(name="lab"))


@pytest.mark.parametrize("mode", ["cluster", "standalone"])
async def test_claim_targets_resolved_plugin_endpoint_and_returns_native_primary_key(
    monkeypatch, mode
):
    _endpoint_rows(monkeypatch, [_endpoint()])
    calls = []

    async def create(nb, path, payload):
        calls.append((path, payload))
        return RestRecord(
            nb,
            path,
            {
                "cluster": {"id": 8, "name": "lab", "type": {"id": 2}},
                "proxmox_cluster": 19,
                "created": True,
            },
        )

    monkeypatch.setattr(cluster_links, "rest_create_async", create)
    claim = await cluster_links.claim_endpoint_cluster(
        object(),
        source=_source(id="cluster/lab"),
        cluster_name="lab",
        mode=mode,
        cluster_type_id=2,
        site_id=4,
        tenant_id=9,
    )
    assert claim.record.id == 8
    assert claim.created is True
    assert calls == [
        (
            f"{cluster_links.ENDPOINT_PATH}5/sync-cluster/",
            {
                "name": "lab",
                "mode": mode,
                "cluster_type": 2,
                "site": 4,
                "tenant": 9,
                "cluster_id": "cluster/lab",
            },
        )
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"cluster": {}, "proxmox_cluster": 19, "created": False},
        {"cluster": {"id": 8}, "proxmox_cluster": 0, "created": False},
        {"cluster": {"id": 8}, "created": False},
    ],
)
async def test_malformed_claim_response_is_fatal(monkeypatch, body):
    _endpoint_rows(monkeypatch, [_endpoint()])

    async def create(nb, path, _payload):
        return RestRecord(nb, path, body)

    monkeypatch.setattr(cluster_links, "rest_create_async", create)
    with pytest.raises((ValidationError, cluster_links.ClusterBindingError)):
        await cluster_links.claim_endpoint_cluster(
            object(),
            source=_source(),
            cluster_name="lab",
            mode="cluster",
            cluster_type_id=2,
        )


async def test_ownership_action_failure_prevents_native_cluster_metadata_write(monkeypatch):
    async def claim(*args, **kwargs):
        raise cluster_links.ClusterBindingError("ownership conflict")

    monkeypatch.setattr(device_ensure, "claim_endpoint_cluster", claim)

    async def native_write(*args, **kwargs):
        pytest.fail("Native cluster mutation preceded ownership claim")

    monkeypatch.setattr(device_ensure, "rest_reconcile_async", native_write)
    with pytest.raises(cluster_links.ClusterBindingError, match="ownership conflict"):
        await device_ensure._ensure_cluster(
            object(),
            cluster_name="lab",
            cluster_type_id=2,
            mode="cluster",
            tag_refs=[],
            endpoint_source=_source(),
        )


@pytest.mark.parametrize("created", [False, True])
async def test_native_metadata_uses_claimed_id_and_preserves_existing_tags(monkeypatch, created):
    native = RestRecord(
        object(),
        cluster_links.CLUSTER_PATH,
        {
            "id": 8,
            "name": "lab",
            "type": 2,
            "tags": [{"slug": "operator"}],
        },
    )

    async def claim(*args, **kwargs):
        assert kwargs["source"].db_endpoint_id == 73
        return cluster_links.ClusterClaim(native, created)

    monkeypatch.setattr(netbox_writers, "claim_endpoint_cluster", claim)

    async def discovery(*args):
        return None

    monkeypatch.setattr(netbox_writers, "resolve_discovery_tag_id", discovery)

    async def reconcile(_nb, path, **kwargs):
        assert path == cluster_links.CLUSTER_PATH
        assert kwargs["lookup"] == {"id": 8}
        assert kwargs["existing_record"] is native
        assert {"slug": "operator"} in kwargs["payload"]["tags"]
        return SimpleNamespace(record=native, status="unchanged")

    monkeypatch.setattr(netbox_writers, "rest_reconcile_async_with_status", reconcile)
    result = await netbox_writers.upsert_cluster(
        object(),
        cluster_name="lab",
        cluster_type_id=2,
        mode="cluster",
        tag_refs=[],
        endpoint_source=_source(),
    )
    assert result.status == ("created" if created else "unchanged")


def test_duplicate_cluster_names_keep_distinct_endpoint_target_clusters():
    states = [
        _source(73, name="lab", node_list=[SimpleNamespace(name="pve1")]),
        _source(74, host="192.0.2.20", name="lab", node_list=[SimpleNamespace(name="pve2")]),
    ]
    clusters = {(73, "lab"): SimpleNamespace(id=8), (74, "lab"): SimpleNamespace(id=9)}
    targets = device_ensure._build_device_targets(states, cluster_by_name=clusters, sites={})
    assert [(target.endpoint_id, target.cluster_id) for target in targets] == [(73, 8), (74, 9)]


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "", "mode": "cluster", "cluster_type": 2},
        {"name": "x" * 101, "mode": "cluster", "cluster_type": 2},
        {"name": "lab", "mode": "invalid", "cluster_type": 2},
        {"name": "lab", "mode": "cluster", "cluster_type": 0},
        {"name": "lab", "mode": "cluster", "cluster_type": True},
        {"name": "lab", "mode": "cluster", "cluster_type": 2, "site": -1},
    ],
)
def test_claim_request_validation_rejects_invalid_native_identity(payload):
    with pytest.raises(ValidationError):
        cluster_links.ClusterClaimRequest.model_validate(payload)


async def test_native_rename_and_retry_keep_using_claimed_primary_key(monkeypatch):
    native = RestRecord(
        object(), cluster_links.CLUSTER_PATH, {"id": 8, "name": "renamed", "type": 2, "tags": []}
    )
    claim_names = []
    metadata_ids = []
    sidecar_ids = []

    async def claim(*args, **kwargs):
        claim_names.append(kwargs["cluster_name"])
        return cluster_links.ClusterClaim(native, False)

    async def reconcile(_nb, _path, **kwargs):
        metadata_ids.append(kwargs["lookup"])
        assert kwargs["existing_record"] is native
        return native

    async def sidecar(_nb, **kwargs):
        sidecar_ids.append(kwargs["cluster_id"])

    monkeypatch.setattr(device_ensure, "claim_endpoint_cluster", claim)
    monkeypatch.setattr(device_ensure, "rest_reconcile_async", reconcile)
    monkeypatch.setattr(device_ensure, "write_cluster_sync_state", sidecar)
    for name in ("lab", "renamed", "renamed"):
        await device_ensure._ensure_cluster(
            object(),
            cluster_name=name,
            cluster_type_id=2,
            mode="standalone",
            tag_refs=[],
            endpoint_source=_source(),
        )
    assert claim_names == ["lab", "renamed", "renamed"]
    assert metadata_ids == [{"id": 8}, {"id": 8}, {"id": 8}]
    assert sidecar_ids == [8, 8, 8]


async def test_bulk_cluster_claims_preserve_both_same_name_endpoint_scopes(monkeypatch):
    sources = [
        _source(73, name="lab", mode="cluster"),
        _source(74, host="192.0.2.20", name="lab", mode="standalone"),
    ]
    claimed = []

    async def cluster_type(_nb, **kwargs):
        return SimpleNamespace(id=3)

    async def site(_nb, **kwargs):
        return SimpleNamespace(id=4)

    async def tenant(_nb, **kwargs):
        return None

    async def cluster(_nb, **kwargs):
        claimed.append((kwargs["endpoint_source"].db_endpoint_id, kwargs["mode"]))
        return SimpleNamespace(id=kwargs["endpoint_source"].db_endpoint_id + 100)

    async def other_dependencies(_nb, phases):
        assert [phase.name for phase in phases] == ["device_types"]
        return {}

    monkeypatch.setattr(device_ensure, "_ensure_cluster_type", cluster_type)
    monkeypatch.setattr(device_ensure, "_ensure_site", site)
    monkeypatch.setattr(device_ensure, "_resolve_tenant", tenant)
    monkeypatch.setattr(device_ensure, "_ensure_cluster", cluster)
    monkeypatch.setattr(device_ensure, "rest_bulk_reconcile_phases_async", other_dependencies)
    _, clusters = await device_ensure._reconcile_cluster_dependencies(
        object(),
        clusters_status=sources,
        cluster_type_by_slug={},
        manufacturer=None,
        tag_refs=[],
        overwrite_flags=None,
    )
    assert claimed == [(73, "cluster"), (74, "standalone")]
    assert {key: value.id for key, value in clusters.items()} == {
        (73, "lab"): 173,
        (74, "lab"): 174,
    }


def test_legacy_vm_dependency_selection_refuses_duplicate_endpoint_cluster_names():
    from proxbox_api.exception import ProxboxException
    from proxbox_api.services.sync.vm_create import _unique_cluster_state

    with pytest.raises(ProxboxException, match="Several endpoint scopes"):
        _unique_cluster_state([SimpleNamespace(name="lab"), SimpleNamespace(name="lab")], "lab")


async def test_individual_dependencies_inherit_standalone_session_mode(monkeypatch):
    from proxbox_api.services.sync.individual.base import BaseIndividualSyncService

    service = BaseIndividualSyncService(object(), SimpleNamespace(mode="standalone"), None)
    modes = []

    async def site(*_args):
        return SimpleNamespace(id=4)

    async def tenant(*_args):
        return None

    async def cluster_type(*_args, **kwargs):
        modes.append(kwargs["mode"])
        return SimpleNamespace(id=2)

    async def cluster(*_args, **kwargs):
        modes.append(kwargs["mode"])
        assert kwargs["endpoint_source"] is service.px
        return SimpleNamespace(id=8)

    monkeypatch.setattr(service, "_get_or_create_site", site)
    monkeypatch.setattr(service, "_get_or_create_tenant", tenant)
    monkeypatch.setattr(device_ensure, "_ensure_cluster_type", cluster_type)
    monkeypatch.setattr(device_ensure, "_ensure_cluster", cluster)
    result = await service._get_or_create_cluster("lab")
    assert result.id == 8
    assert modes == ["standalone", "standalone"]
