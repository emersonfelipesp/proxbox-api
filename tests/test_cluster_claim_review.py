"""Adversarial identity and actual-outcome regressions for cluster ownership."""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import RestRecord
from proxbox_api.services import netbox_writers
from proxbox_api.services.sync import cluster_links, device_ensure
from proxbox_api.services.sync.individual import cluster_sync
from proxbox_api.session import proxmox_core
from tests.factories.session import make_session, make_settings


def _source(**values):
    return SimpleNamespace(
        **{
            "name": "lab",
            "domain": None,
            "ip_address": "192.0.2.10",
            "http_port": 8006,
            "db_endpoint_id": 73,
            "endpoint_source": "database",
            "mode": "cluster",
            **values,
        }
    )


def _context(sources):
    return make_session(
        nb=object(),
        px_sessions=sources,
        tag=None,
        settings=make_settings(),
        operation_id="cluster-review",
    )


async def _fail_write(*args, **kwargs):
    pytest.fail("A write occurred before exact ownership was established")


@pytest.mark.parametrize(
    "source_domain,row_domain", [(None, "other.example.test"), ("other.example.test", None)]
)
async def test_unused_matching_ip_cannot_override_different_effective_hosts(
    monkeypatch, source_domain, row_domain
):
    async def rows(*args):
        return [
            {
                "id": 5,
                "domain": row_domain,
                "ip_address": {"address": "192.0.2.10/32"},
                "port": 8006,
            }
        ]

    monkeypatch.setattr(cluster_links, "rest_list_async", rows)
    monkeypatch.setattr(cluster_links, "rest_create_async", _fail_write)
    with pytest.raises(cluster_links.ClusterBindingError):
        await cluster_links.claim_endpoint_cluster(
            object(),
            source=_source(domain=source_domain),
            cluster_name="lab",
            mode="cluster",
            cluster_type_id=2,
        )


@pytest.mark.parametrize(
    "source,row",
    [
        (
            _source(domain="192.0.2.10"),
            {"ip_address": {"address": "192.0.2.10/32"}},
        ),
        (_source(), {"domain": "192.0.2.10", "ip_address": {"address": "192.0.2.99/32"}}),
        (
            _source(domain="PVE.EXAMPLE.TEST."),
            {"domain": "pve.example.test", "ip_address": {"address": "192.0.2.10/32"}},
        ),
    ],
)
def test_effective_host_precedence_allows_normalized_cross_field_identity(source, row):
    assert cluster_links._matches_transport(row, source)


@pytest.mark.parametrize("endpoint_source", ["database", "netbox"])
@pytest.mark.parametrize(
    "domain,source_ip,row_domain,row_ip,allowed",
    [
        ("pve.example.test", "192.0.2.10", "pve.example.test", "192.0.2.20", False),
        ("192.0.2.10", "192.0.2.20", None, "192.0.2.10", False),
        ("pve.example.test", "192.0.2.10", "pve.example.test", "192.0.2.10", True),
        (None, "192.0.2.10", "192.0.2.10", "192.0.2.20", True),
    ],
)
async def test_actual_authentication_fallback_requires_endpoint_corroboration(
    monkeypatch, endpoint_source, domain, source_ip, row_domain, row_ip, allowed
):
    attempts = []
    claims = []

    def sdk(host, **kwargs):
        attempts.append(host)
        if domain and host == domain:
            raise RuntimeError("Synthetic preferred-host failure")
        return SimpleNamespace(host=host, version=SimpleNamespace(get=lambda: {"version": "8.4"}))

    async def rows(*args):
        return [{"id": 5, "domain": row_domain, "ip_address": {"address": f"{row_ip}/32"}}]

    async def action(nb, path, payload):
        claims.append(path)
        return RestRecord(nb, path, {"cluster": {"id": 8}, "proxmox_cluster": 19, "created": False})

    source = proxmox_core.ProxmoxSession()
    source.domain = domain
    source.ip_address = source_ip
    source.db_endpoint_id = 5 if endpoint_source == "netbox" else 73
    source.endpoint_source = endpoint_source
    monkeypatch.setattr(proxmox_core, "_proxmox_api_factory", lambda: sdk)
    connected = await source._auth_async()
    assert connected.host == source_ip
    assert attempts == ([domain, source_ip] if domain else [source_ip])
    assert source.domain == domain
    monkeypatch.setattr(cluster_links, "rest_list_async", rows)
    monkeypatch.setattr(cluster_links, "rest_create_async", action)
    monkeypatch.setattr(netbox_writers, "rest_reconcile_async_with_status", _fail_write)
    if allowed:
        claim = await cluster_links.claim_endpoint_cluster(
            object(), source=source, cluster_name="lab", mode="cluster", cluster_type_id=2
        )
        assert claim.record.id == 8
        assert claims == ["/api/plugins/proxbox/endpoints/proxmox/5/sync-cluster/"]
    else:
        with pytest.raises(cluster_links.ClusterBindingError):
            await netbox_writers.upsert_cluster(
                object(),
                cluster_name="lab",
                cluster_type_id=2,
                mode="cluster",
                tag_refs=[],
                endpoint_source=source,
            )
        assert claims == []


@pytest.mark.parametrize(
    "cluster",
    [
        {"id": True},
        {"id": "8"},
        {"id": 8.0},
        {"id": -1},
        {"id": 0},
        {"value": 8},
        {"id": 8, "url": "/api/virtualization/clusters/9/"},
        {"id": 8, "url": "https://netbox.example/api/virtualization/clusters/9/"},
        {"id": 8, "url": "/api/virtualization/clusters/8/?other=9"},
        {"id": 8, "url": "/api/virtualization/clusters/8/#other"},
        {"id": 8, "url": "ftp://netbox.example/api/virtualization/clusters/8/"},
        {"id": 8, "url": "javascript:invalid"},
        {"id": 8, "url": 9},
    ],
)
async def test_malformed_native_identity_blocks_metadata_patch(monkeypatch, cluster):
    async def rows(*args):
        return [{"id": 5, "ip_address": {"address": "192.0.2.10/32"}}]

    async def action(nb, path, payload):
        return RestRecord(nb, path, {"cluster": cluster, "proxmox_cluster": 19, "created": False})

    monkeypatch.setattr(cluster_links, "rest_list_async", rows)
    monkeypatch.setattr(cluster_links, "rest_create_async", action)
    monkeypatch.setattr(netbox_writers, "rest_reconcile_async_with_status", _fail_write)
    with pytest.raises((ValidationError, cluster_links.ClusterBindingError)):
        await netbox_writers.upsert_cluster(
            object(),
            cluster_name="lab",
            cluster_type_id=2,
            mode="cluster",
            tag_refs=[],
            endpoint_source=_source(),
        )


async def test_canonical_claim_record_save_targets_validated_id(monkeypatch):
    calls = []

    async def rows(*args):
        return [{"id": 5, "ip_address": {"address": "192.0.2.10/32"}}]

    async def action(nb, path, payload):
        return RestRecord(
            nb,
            path,
            {
                "cluster": {
                    "id": 8,
                    "url": "https://netbox.example/api/virtualization/clusters/8/",
                },
                "proxmox_cluster": 19,
                "created": False,
            },
        )

    async def request(method, path, **kwargs):
        from netbox_sdk.client import ApiResponse

        calls.append((method, path))
        return ApiResponse(status=200, text=json.dumps({"id": 8, "name": "lab"}))

    nb = SimpleNamespace(client=SimpleNamespace(request=request))
    monkeypatch.setattr(cluster_links, "rest_list_async", rows)
    monkeypatch.setattr(cluster_links, "rest_create_async", action)
    claim = await cluster_links.claim_endpoint_cluster(
        nb, source=_source(), cluster_name="lab", mode="cluster", cluster_type_id=2
    )
    claim.record.name = "lab"
    await claim.record.save()
    assert calls == [("PATCH", "/api/virtualization/clusters/8/")]
    assert claim.changed is False


@pytest.mark.parametrize("ids", [(None, None), (73, 73), (True, 74), (0, 74)])
async def test_invalid_bulk_scopes_fail_before_all_prerequisite_writes(monkeypatch, ids):
    states = [
        _source(db_endpoint_id=ids[0]),
        _source(db_endpoint_id=ids[1], ip_address="192.0.2.20"),
    ]
    monkeypatch.setattr(device_ensure, "_reconcile_base_device_dependencies", _fail_write)
    monkeypatch.setattr(device_ensure, "_ensure_cluster_type", _fail_write)
    monkeypatch.setattr(device_ensure, "_ensure_site", _fail_write)
    monkeypatch.setattr(device_ensure, "_ensure_cluster", _fail_write)
    with pytest.raises(cluster_links.ClusterBindingError):
        await device_ensure.ensure_proxmox_devices_bulk(
            object(), clusters_status=states, tag_refs=[]
        )
    with pytest.raises(cluster_links.ClusterBindingError):
        await device_ensure._reconcile_cluster_dependencies(
            object(),
            clusters_status=states,
            cluster_type_by_slug={},
            manufacturer=None,
            tag_refs=[],
            overwrite_flags=None,
        )


@pytest.mark.parametrize("dry_run", [False, True])
async def test_ambiguous_individual_session_refuses_before_any_write(monkeypatch, dry_run):
    sources = [_source(), _source(db_endpoint_id=74, ip_address="192.0.2.20", name="LAB")]
    monkeypatch.setattr(cluster_sync, "upsert_cluster_type", _fail_write)
    monkeypatch.setattr(cluster_sync, "upsert_cluster", _fail_write)
    monkeypatch.setattr(cluster_sync, "resolve_plugin_endpoint_id", _fail_write)
    with pytest.raises(ProxboxException, match="Several endpoint scopes"):
        await cluster_sync.sync_cluster_individual(_context(sources), "lab", dry_run=dry_run)


@pytest.mark.parametrize("linked", [False, True])
async def test_cluster_dry_run_is_exact_and_write_free(monkeypatch, linked):
    queries = []

    async def resolve(*args):
        return 5

    async def rows(nb, path, *, query):
        queries.append((path, query))
        if path == cluster_links.ENDPOINT_PATH:
            return [{"id": 5, "netbox_cluster": {"id": 8} if linked else None}]
        return [{"id": 8, "name": "lab"}]

    monkeypatch.setattr(cluster_sync, "resolve_plugin_endpoint_id", resolve)
    monkeypatch.setattr(cluster_sync, "rest_list_async", rows)
    monkeypatch.setattr(cluster_sync, "upsert_cluster_type", _fail_write)
    monkeypatch.setattr(cluster_sync, "upsert_cluster", _fail_write)
    monkeypatch.setattr(cluster_links, "claim_endpoint_cluster", _fail_write)
    monkeypatch.setattr(cluster_links, "rest_create_async", _fail_write)
    result = await cluster_sync.sync_cluster_individual(_context([_source()]), "lab", dry_run=True)
    assert result["action"] == "dry_run"
    assert result["netbox_object"] == ({"id": 8, "name": "lab"} if linked else None)
    assert queries[0] == (cluster_links.ENDPOINT_PATH, {"id": 5, "limit": 2})
    assert len(queries) == (2 if linked else 1)


@pytest.mark.parametrize(
    "endpoint_id,core_rows",
    [(6, [{"id": 8}]), (5, [{"id": 9}]), (5, [{"id": 8}, {"id": 8}]), (5, [{"value": 8}])],
)
async def test_dry_run_rejects_unverified_returned_identity(monkeypatch, endpoint_id, core_rows):
    async def resolve(*args):
        return 5

    async def rows(nb, path, **kwargs):
        return (
            [{"id": endpoint_id, "netbox_cluster": {"id": 8}}]
            if path == cluster_links.ENDPOINT_PATH
            else core_rows
        )

    monkeypatch.setattr(cluster_sync, "resolve_plugin_endpoint_id", resolve)
    monkeypatch.setattr(cluster_sync, "rest_list_async", rows)
    monkeypatch.setattr(cluster_sync, "upsert_cluster", _fail_write)
    with pytest.raises(cluster_links.ClusterBindingError):
        await cluster_sync.sync_cluster_individual(_context([_source()]), "lab", dry_run=True)


@pytest.mark.parametrize("old_name,old_type", [("old-name", 2), ("lab", 3)])
async def test_real_claim_change_reports_updated_then_unchanged_retry(
    monkeypatch, old_name, old_type
):
    stored = {
        "id": 8,
        "name": old_name,
        "type": old_type,
        "tags": [],
        "description": "Proxmox cluster cluster.",
    }
    counters = {"claims": 0, "native_updates": 0}

    async def rows(*args):
        return [{"id": 5, "ip_address": {"address": "192.0.2.10/32"}}]

    async def action(nb, path, payload):
        counters["claims"] += 1
        changed = (stored["name"], stored["type"]) != (payload["name"], payload["cluster_type"])
        counters["native_updates"] += int(changed)
        stored.update(name=payload["name"], type=payload["cluster_type"])
        return RestRecord(
            nb,
            path,
            {"cluster": dict(stored), "proxmox_cluster": 19, "created": False, "changed": changed},
        )

    async def cluster_type(*args, **kwargs):
        return SimpleNamespace(record=SimpleNamespace(id=2), status="unchanged")

    monkeypatch.setattr(cluster_links, "rest_list_async", rows)
    monkeypatch.setattr(cluster_links, "rest_create_async", action)
    monkeypatch.setattr(cluster_sync, "upsert_cluster_type", cluster_type)
    context = _context([_source()])
    first = await cluster_sync.sync_cluster_individual(context, "lab")
    second = await cluster_sync.sync_cluster_individual(context, "lab")
    assert [first["action"], second["action"]] == ["updated", "unchanged"]
    assert counters == {"claims": 2, "native_updates": 1}
    assert first["netbox_object"]["id"] == second["netbox_object"]["id"] == 8


@pytest.mark.parametrize("changed", [None, "true", 1])
def test_claim_change_outcome_requires_strict_boolean(changed):
    with pytest.raises(ValidationError):
        cluster_links.ClusterClaimResponse.model_validate(
            {
                "cluster": {"id": 8},
                "proxmox_cluster": 19,
                "created": False,
                "changed": changed,
            }
        )


@pytest.mark.parametrize("writer", ["individual", "dependencies"])
async def test_old_plugin_missing_action_fails_before_native_metadata(monkeypatch, writer):
    from netbox_sdk.client import ApiResponse

    calls = []

    async def rows(*args):
        return [{"id": 5, "ip_address": {"address": "192.0.2.10/32"}}]

    async def request(method, path, **kwargs):
        calls.append((method, path))
        return ApiResponse(status=404, text=json.dumps({"detail": "Not found."}))

    monkeypatch.setattr(cluster_links, "rest_list_async", rows)
    monkeypatch.setattr(netbox_writers, "rest_reconcile_async_with_status", _fail_write)
    monkeypatch.setattr(device_ensure, "rest_reconcile_async", _fail_write)
    nb = SimpleNamespace(client=SimpleNamespace(request=request))
    operation = (
        netbox_writers.upsert_cluster if writer == "individual" else device_ensure._ensure_cluster
    )
    with pytest.raises(ProxboxException):
        await operation(
            nb,
            cluster_name="lab",
            cluster_type_id=2,
            mode="cluster",
            tag_refs=[],
            endpoint_source=_source(),
        )
    assert calls == [("POST", "/api/plugins/proxbox/endpoints/proxmox/5/sync-cluster/")]
