"""Read-only cluster adoption for netbox-proxbox plugins without the claim action.

The tracking rows model a NetBox where the plugin has no ``sync-cluster``
action and three endpoints report the same cluster name.
"""

from types import SimpleNamespace

import pytest

from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import RestProbe, RestRecord, rest_get_fresh_json, rest_probe_async
from proxbox_api.services import netbox_writers
from proxbox_api.services.sync import cluster_links, device_ensure

HTML_404 = RestProbe(status=404, content_type="text/html; charset=utf-8")

TRACKING = [
    {"id": 3, "name": "PVE-CLUSTER-02", "endpoint": {"id": 2}, "netbox_cluster": None},
    {"id": 6, "name": "TEST-CLUSTER", "endpoint": {"id": 2}, "netbox_cluster": None},
    {"id": 9, "name": "dc01-cluster", "endpoint": {"id": 5}, "netbox_cluster": {"id": 8}},
    {"id": 8, "name": "node01", "endpoint": {"id": 5}, "netbox_cluster": None},
    {"id": 4, "name": "PVE-CLUSTER-02", "endpoint": {"id": 4}, "netbox_cluster": None},
    {"id": 7, "name": "TEST-CLUSTER", "endpoint": {"id": 4}, "netbox_cluster": None},
    {"id": 5, "name": "PVE-CLUSTER-02", "endpoint": {"id": 3}, "netbox_cluster": None},
    {"id": 1, "name": "pve01", "endpoint": {"id": 3}, "netbox_cluster": {"id": 3}},
    {"id": 10, "name": "pve-branch-01", "endpoint": {"id": 10}, "netbox_cluster": {"id": 9}},
    {"id": 11, "name": "vpve", "endpoint": {"id": 11}, "netbox_cluster": {"id": 11}},
]

CLUSTERS = {
    3: {"id": 3, "name": "pve01"},
    4: {"id": 4, "name": "TEST-CLUSTER"},
    6: {"id": 6, "name": "PVE-CLUSTER-02"},
    8: {"id": 8, "name": "dc01-cluster"},
    11: {"id": 11, "name": "vpve", "tags": []},
}


def _source():
    return SimpleNamespace(
        name="vpve",
        domain=None,
        ip_address="192.0.2.10",
        http_port=8006,
        db_endpoint_id=73,
        endpoint_source="database",
        mode="standalone",
    )


async def _fail_write(*args, **kwargs):
    pytest.fail("A write occurred on the read-only adoption path")


def _page(rows):
    return {"count": len(rows), "next": None, "previous": None, "results": list(rows)}


def _install(
    monkeypatch,
    *,
    endpoint_id=11,
    tracking=None,
    tracking_page=None,
    clusters=None,
    endpoint_enabled=True,
    endpoint_rows=None,
    endpoint_page=None,
    auth_user=None,
    superusers=(7,),
    users_filter_ignored=False,
    users_hidden=False,
    probe=HTML_404,
):
    """Patch the REST seams. Fresh reads serve the adoption; lists serve resolution."""
    calls = {"lists": [], "probes": [], "fresh": []}
    tracking = TRACKING if tracking is None else tracking
    clusters = CLUSTERS if clusters is None else clusters

    async def lists(nb, path, *, query=None):
        calls["lists"].append((path, query))
        if path != cluster_links.ENDPOINT_PATH:
            pytest.fail(f"The adoption path used a cached list read: {path}")
        return [{"id": endpoint_id, "ip_address": {"address": "192.0.2.10/32"}}]

    fresh_endpoints = (
        endpoint_rows
        if endpoint_rows is not None
        else [
            {
                "id": endpoint_id,
                "ip_address": {"address": "192.0.2.10/32"},
                "enabled": endpoint_enabled,
            }
        ]
    )

    async def fresh(nb, path, *, query=None):
        calls["fresh"].append((path, query))
        if path == cluster_links.AUTH_CHECK_PATH:
            return {"id": 7, "username": "svc"} if auth_user is None else auth_user
        if path == cluster_links.USERS_PATH:
            wanted = query["is_superuser"] == "true"
            rows = [
                {"id": query["id"]}
                for _ in [0]
                if not users_hidden
                and (users_filter_ignored or (query["id"] in superusers) == wanted)
            ]
            return _page(rows)
        if path == cluster_links.ENDPOINT_PATH:
            return endpoint_page if endpoint_page is not None else _page(fresh_endpoints)
        if path == cluster_links.TRACKING_PATH:
            return tracking_page if tracking_page is not None else _page(tracking)
        cluster_id = int(path.rstrip("/").rsplit("/", 1)[1])
        if cluster_id not in clusters:
            raise ProxboxException(message="NetBox REST request failed", http_status_code=404)
        return clusters[cluster_id]

    async def fake_probe(nb, path):
        calls["probes"].append(path)
        if isinstance(probe, BaseException):
            raise probe
        return probe

    monkeypatch.setattr(cluster_links, "rest_list_async", lists)
    monkeypatch.setattr(cluster_links, "rest_get_fresh_json", fresh)
    monkeypatch.setattr(cluster_links, "rest_probe_async", fake_probe)
    monkeypatch.setattr(cluster_links, "rest_create_async", _fail_write)
    return calls


async def _claim(cluster_name="vpve"):
    return await cluster_links.claim_endpoint_cluster(
        object(), source=_source(), cluster_name=cluster_name, mode="standalone", cluster_type_id=2
    )


async def test_html_404_adopts_the_tracking_linked_cluster_read_only(monkeypatch):
    calls = _install(monkeypatch)
    claim = await _claim()
    assert claim.record.id == 11
    assert claim.record.url == "/api/virtualization/clusters/11/"
    assert (claim.created, claim.changed, claim.read_only) == (False, False, True)
    assert calls["probes"] == ["/api/plugins/proxbox/endpoints/proxmox/11/sync-cluster/"]
    assert calls["fresh"] == [
        ("/api/authentication-check/", None),
        ("/api/users/users/", {"limit": 1000, "id": 7, "is_superuser": "true"}),
        ("/api/users/users/", {"limit": 1000, "id": 7, "is_superuser": "false"}),
        (cluster_links.ENDPOINT_PATH, {"limit": 1000}),
        (cluster_links.TRACKING_PATH, {"limit": 1000}),
        ("/api/virtualization/clusters/11/", None),
    ]


async def test_endpoint_with_an_unrelated_unlinked_row_still_adopts(monkeypatch):
    _install(monkeypatch, endpoint_id=5)
    claim = await _claim("dc01-cluster")
    assert claim.record.id == 8


@pytest.mark.parametrize(
    "endpoint_id,cluster_name",
    [
        (2, "PVE-CLUSTER-02"),
        (4, "TEST-CLUSTER"),
        (3, "PVE-CLUSTER-02"),
        (11, "VPVE"),
        (11, "absent"),
    ],
)
async def test_unlinked_missing_or_case_different_rows_refuse(
    monkeypatch, endpoint_id, cluster_name
):
    _install(monkeypatch, endpoint_id=endpoint_id)
    with pytest.raises(cluster_links.ClusterBindingError):
        await _claim(cluster_name)


async def test_names_shared_across_endpoints_never_merge_into_one_cluster(monkeypatch):
    # Every endpoint's PVE-CLUSTER-02 row links native cluster 6. Only the
    # other-endpoint check refuses, so removing it makes this test fail.
    shared = [
        {**row, "netbox_cluster": {"id": 6}} if row["name"] == "PVE-CLUSTER-02" else row
        for row in TRACKING
    ]
    _install(monkeypatch, endpoint_id=2, tracking=shared)
    with pytest.raises(cluster_links.ClusterBindingError):
        await _claim("PVE-CLUSTER-02")


@pytest.mark.parametrize(
    "extra",
    [
        {"id": 12, "name": "vpve-old", "endpoint": {"id": 11}, "netbox_cluster": {"id": 4}},
        {"id": 13, "name": "vpve", "endpoint": {"id": 11}, "netbox_cluster": {"id": 11}},
        {"id": 14, "name": "vpve", "netbox_cluster": {"id": 11}},
    ],
)
async def test_ambiguous_tracking_rows_refuse(monkeypatch, extra):
    _install(monkeypatch, tracking=[*TRACKING, extra])
    with pytest.raises(cluster_links.ClusterBindingError):
        await _claim()


@pytest.mark.parametrize(
    "page",
    [
        {**_page(TRACKING), "next": "/api/plugins/proxbox/proxmox-clusters/?offset=1000"},
        {**_page(TRACKING), "count": len(TRACKING) + 1},
        {"results": "not-a-list", "count": 0, "next": None},
        [],
    ],
)
async def test_incomplete_or_malformed_tracking_page_refuses(monkeypatch, page):
    _install(monkeypatch, tracking_page=page)
    with pytest.raises(cluster_links.ClusterBindingError):
        await _claim()


@pytest.mark.parametrize("enabled", [False, None])
async def test_endpoint_disabled_after_resolution_refuses(monkeypatch, enabled):
    _install(monkeypatch, endpoint_enabled=enabled)
    with pytest.raises(cluster_links.ClusterBindingError):
        await _claim()


@pytest.mark.parametrize(
    "native",
    [
        {"id": 11, "name": "vpve-renamed"},
        {"id": 3, "name": "vpve"},
        {"id": 11, "name": "vpve", "url": "/api/virtualization/clusters/99/"},
        {"id": "11", "name": "vpve"},
        {"id": 0, "name": "vpve"},
        {"name": "vpve"},
    ],
)
async def test_native_cluster_identity_must_match_exactly(monkeypatch, native):
    _install(monkeypatch, clusters={**CLUSTERS, 11: native})
    with pytest.raises(cluster_links.ClusterBindingError):
        await _claim()


async def test_missing_native_cluster_refuses(monkeypatch):
    _install(monkeypatch, clusters={k: v for k, v in CLUSTERS.items() if k != 11})
    with pytest.raises(cluster_links.ClusterBindingError):
        await _claim()


@pytest.mark.parametrize(
    "probe",
    [
        RestProbe(status=404, content_type="application/json"),
        RestProbe(status=404, content_type=""),
        RestProbe(status=405, content_type="application/json"),
        RestProbe(status=403, content_type="text/html; charset=utf-8"),
        RestProbe(status=200, content_type="text/html"),
        RestProbe(status=502, content_type="text/html"),
        TimeoutError(),
        RuntimeError("transport failure"),
    ],
)
async def test_anything_but_an_html_404_keeps_the_atomic_claim(monkeypatch, probe):
    calls = _install(monkeypatch, probe=probe)
    posted = []

    async def action(nb, path, payload):
        posted.append((path, payload))
        return RestRecord(
            nb, path, {"cluster": {"id": 11}, "proxmox_cluster": 19, "created": False}
        )

    monkeypatch.setattr(cluster_links, "rest_create_async", action)
    claim = await _claim()
    assert (claim.record.id, claim.read_only) == (11, False)
    assert calls["fresh"] == []
    assert posted == [
        (
            "/api/plugins/proxbox/endpoints/proxmox/11/sync-cluster/",
            {"name": "vpve", "mode": "standalone", "cluster_type": 2},
        )
    ]


async def test_html_content_type_match_ignores_case_and_parameters(monkeypatch):
    _install(monkeypatch, probe=RestProbe(status=404, content_type=" Text/HTML ;charset=UTF-8"))
    claim = await _claim()
    assert claim.record.id == 11


async def test_invalid_request_is_rejected_before_the_probe(monkeypatch):
    calls = _install(monkeypatch)
    with pytest.raises(ValueError):
        await cluster_links.claim_endpoint_cluster(
            object(), source=_source(), cluster_name="vpve", mode="other", cluster_type_id=2
        )
    assert calls["probes"] == []


def _bounded_client(response, requests):
    async def request_bounded(method, path, *, max_response_bytes, query=None):
        requests.append((method, path, query, max_response_bytes))
        return response

    return SimpleNamespace(client=SimpleNamespace(request_bounded=request_bounded))


async def test_probe_uses_one_bounded_uncached_request_and_reads_any_header_case():
    from netbox_sdk.client import ApiResponse

    requests = []
    nb = _bounded_client(
        ApiResponse(status=404, text="<!DOCTYPE html>", headers={"content-TYPE": "text/html"}),
        requests,
    )
    probe = await rest_probe_async(nb, "api/plugins/proxbox/endpoints/proxmox/11/sync-cluster")
    assert probe == RestProbe(status=404, content_type="text/html")
    assert requests == [
        ("GET", "/api/plugins/proxbox/endpoints/proxmox/11/sync-cluster/", None, 1_048_576)
    ]


async def test_fresh_json_read_is_bounded_uncached_and_status_checked():
    from netbox_sdk.client import ApiResponse

    requests = []
    nb = _bounded_client(ApiResponse(status=200, text='{"id": 11}'), requests)
    assert await rest_get_fresh_json(nb, "/api/virtualization/clusters/11", query={"a": 1}) == {
        "id": 11
    }
    assert requests == [("GET", "/api/virtualization/clusters/11/", {"a": 1}, 1_048_576)]
    failing = _bounded_client(ApiResponse(status=404, text='{"detail": "Not found."}'), [])
    with pytest.raises(ProxboxException):
        await rest_get_fresh_json(failing, "/api/virtualization/clusters/12/")


async def test_individual_cluster_writer_never_writes_an_adopted_cluster(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(netbox_writers, "rest_reconcile_async_with_status", _fail_write)
    result = await netbox_writers.upsert_cluster(
        object(),
        cluster_name="vpve",
        cluster_type_id=2,
        mode="standalone",
        tag_refs=[],
        endpoint_source=_source(),
    )
    assert (result.status, result.record.id) == ("unchanged", 11)


async def test_dependency_writer_never_writes_an_adopted_cluster(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(device_ensure, "rest_reconcile_async", _fail_write)
    monkeypatch.setattr(device_ensure, "write_cluster_sync_state", _fail_write)
    cluster = await device_ensure._ensure_cluster(
        object(),
        cluster_name="vpve",
        cluster_type_id=2,
        mode="standalone",
        tag_refs=[],
        endpoint_source=_source(),
    )
    assert cluster.id == 11


async def test_adopted_record_url_is_rebuilt_from_the_validated_id(monkeypatch):
    absolute = {
        "id": 11,
        "name": "vpve",
        "url": "https://netbox.example/api/virtualization/clusters/11/",
    }
    _install(monkeypatch, clusters={**CLUSTERS, 11: absolute})
    claim = await _claim()
    assert claim.record.url == "/api/virtualization/clusters/11/"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"superusers": ()},
        {"users_filter_ignored": True},
        {"users_hidden": True},
    ],
)
async def test_adoption_requires_a_proven_superuser_token(monkeypatch, kwargs):
    calls = _install(monkeypatch, **kwargs)
    with pytest.raises(cluster_links.ClusterBindingError):
        await _claim()
    assert cluster_links.TRACKING_PATH not in [path for path, _query in calls["fresh"]]


async def test_hidden_owner_risk_stops_before_any_dependency_write(monkeypatch):
    # Without proof of a complete view, another endpoint's row may be hidden; no
    # cluster, sidecar, or device work may start.
    _install(monkeypatch, superusers=())
    monkeypatch.setattr(device_ensure, "rest_reconcile_async", _fail_write)
    monkeypatch.setattr(device_ensure, "write_cluster_sync_state", _fail_write)
    with pytest.raises(cluster_links.ClusterBindingError):
        await device_ensure._ensure_cluster(
            object(),
            cluster_name="vpve",
            cluster_type_id=2,
            mode="standalone",
            tag_refs=[],
            endpoint_source=_source(),
        )


@pytest.mark.parametrize(
    "fresh_row",
    [
        {"id": 11, "domain": "other-pve.example.test", "ip_address": {"address": "192.0.2.10/32"}},
        {"id": 11, "ip_address": {"address": "192.0.2.10/32"}, "port": 8007},
        {"id": 11, "ip_address": {"address": "192.0.2.99/32"}},
    ],
)
async def test_changed_transport_identity_refuses(monkeypatch, fresh_row):
    _install(monkeypatch, endpoint_rows=[{**fresh_row, "enabled": True}])
    with pytest.raises(cluster_links.ClusterBindingError):
        await _claim()


async def test_second_endpoint_with_the_same_transport_refuses(monkeypatch):
    rows = [
        {"id": 11, "ip_address": {"address": "192.0.2.10/32"}, "enabled": True},
        {"id": 12, "ip_address": {"address": "192.0.2.10/32"}, "enabled": True},
    ]
    _install(monkeypatch, endpoint_rows=rows)
    with pytest.raises(cluster_links.ClusterBindingError):
        await _claim()


async def test_fresh_resolution_must_pick_the_same_endpoint(monkeypatch):
    rows = [{"id": 12, "ip_address": {"address": "192.0.2.10/32"}, "enabled": True}]
    _install(monkeypatch, endpoint_rows=rows)
    with pytest.raises(cluster_links.ClusterBindingError):
        await _claim()


async def test_paginated_endpoint_page_refuses(monkeypatch):
    row = {"id": 11, "ip_address": {"address": "192.0.2.10/32"}, "enabled": True}
    page = {**_page([row]), "next": "/api/plugins/proxbox/endpoints/proxmox/?offset=1000"}
    _install(monkeypatch, endpoint_page=page)
    with pytest.raises(cluster_links.ClusterBindingError):
        await _claim()


@pytest.mark.parametrize("auth_user", [{"id": "7"}, {"id": True}, {"id": 0}, {"id": -7}, {}])
async def test_malformed_authenticated_user_id_refuses_before_any_user_query(
    monkeypatch, auth_user
):
    calls = _install(monkeypatch, auth_user=auth_user, superusers=("7", True, 0, -7))
    with pytest.raises(cluster_links.ClusterBindingError):
        await _claim()
    assert cluster_links.USERS_PATH not in [path for path, _query in calls["fresh"]]
