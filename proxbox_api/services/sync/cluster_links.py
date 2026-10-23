"""Claim endpoint-owned native clusters through the plugin's atomic action.

netbox-proxbox releases before 0.0.31rc2 have no ``sync-cluster`` action. For
them only, an existing native cluster that the plugin's own tracking rows
already bind to the endpoint is adopted read-only; nothing is ever created,
linked, renamed, or matched by name across endpoints without the action.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_interface
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    RestRecord,
    rest_create_async,
    rest_get_fresh_json,
    rest_list_async,
    rest_probe_async,
)
from proxbox_api.services.sync.vm_helpers import record_id, to_mapping

ENDPOINT_PATH = "/api/plugins/proxbox/endpoints/proxmox/"
CLUSTER_PATH = "/api/virtualization/clusters/"
TRACKING_PATH = "/api/plugins/proxbox/proxmox-clusters/"
CLAIM_ACTION = "sync-cluster"
_LEGACY_ADOPTION_REFUSED = (
    "netbox-proxbox has no sync-cluster action, and its tracking rows do not bind this "
    "endpoint to exactly one existing native cluster with this name. Upgrade "
    "netbox-proxbox to 0.0.31 or later so the endpoint can claim its cluster."
)


class ClusterBindingError(RuntimeError):
    """The endpoint identity or atomic ownership response could not be verified."""


class NativeClusterResponse(BaseModel):
    """Require a native primary key and reject conflicting detail identity."""

    model_config = ConfigDict(extra="allow")
    id: int = Field(gt=0, strict=True)
    url: str | None = None

    @model_validator(mode="after")
    def validate_detail_identity(self) -> NativeClusterResponse:
        if self.url is not None:
            parsed = urlsplit(self.url)
            if (
                parsed.path != f"{CLUSTER_PATH}{self.id}/"
                or parsed.query
                or parsed.fragment
                or parsed.scheme not in {"", "http", "https"}
            ):
                raise ValueError("Claimed native URL does not identify the validated cluster ID.")
        return self


class ClusterClaimResponse(BaseModel):
    """Validated response from the plugin's endpoint-scoped transaction."""

    model_config = ConfigDict(extra="ignore")
    cluster: NativeClusterResponse
    proxmox_cluster: int = Field(gt=0, strict=True)
    created: StrictBool
    changed: StrictBool = False


class ClusterClaimRequest(BaseModel):
    """Exact wire input for the plugin-owned cluster transaction."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    mode: Literal["cluster", "standalone"]
    cluster_type: int = Field(gt=0, strict=True)
    site: int | None = Field(default=None, gt=0, strict=True)
    tenant: int | None = Field(default=None, gt=0, strict=True)
    cluster_id: str | None = Field(default=None, max_length=255)


@dataclass(frozen=True)
class ClusterClaim:
    record: RestRecord
    created: bool
    changed: bool = False
    # True for the compatibility adoption: callers must not write the native
    # cluster or its sync-state sidecar, because no ownership claim backs it.
    read_only: bool = False


def _value(source: object, key: str) -> object:
    return source.get(key) if isinstance(source, dict) else getattr(source, key, None)


def _host(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("address")
    text = str(value or "").strip().lower().rstrip(".")
    if not text:
        return ""
    try:
        return str(ip_interface(text).ip)
    except ValueError:
        return text


def _effective_host(source: object) -> str:
    """Use the transports' actual domain-before-IP connection precedence."""
    return _host(_value(source, "domain") or _value(source, "ip_address"))


def _matches_source_fallback(row: object, source: object) -> bool:
    """Corroborate every alternate host the source can actually connect to."""
    fallback = _host(_value(source, "ip_address"))
    if not _value(source, "domain") or not fallback:
        return True
    return fallback in {_effective_host(row), _host(_value(row, "ip_address"))}


def _matches_transport(row: object, source: object) -> bool:
    """Require an exact nonempty host and reject contradictory transport fields."""
    expected = _effective_host(source)
    actual = _effective_host(row)
    source_port = _value(source, "http_port") or _value(source, "port") or 8006
    return (
        bool(expected and expected == actual)
        and _matches_source_fallback(row, source)
        and str(source_port) == str(_value(row, "port") or 8006)
    )


def _select_plugin_endpoint(rows: list[object], source: object) -> object:
    """Select the one endpoint row whose transport identity matches the session source."""
    endpoint_source = _value(source, "endpoint_source")
    if endpoint_source == "netbox":
        configured_id = record_id({"id": _value(source, "db_endpoint_id")})
        rows = [row for row in rows if record_id(row) == configured_id]
    candidates = [row for row in rows if _matches_transport(row, source)]
    if len(candidates) != 1 or not record_id(candidates[0]):
        raise ClusterBindingError(
            "Cannot resolve one exact NetBox ProxmoxEndpoint from the session transport "
            "identity. Check endpoint source, host, port, and duplicate endpoint records."
        )
    return candidates[0]


async def resolve_plugin_endpoint_id(nb: object, source: object) -> int:
    """Resolve exact plugin ownership without conflating independent ID spaces."""
    row = _select_plugin_endpoint(list(await rest_list_async(nb, ENDPOINT_PATH)), source)
    if _value(row, "enabled") is False:
        raise ClusterBindingError("The resolved NetBox ProxmoxEndpoint is disabled.")
    return int(record_id(row))


async def _claim_action_absent(nb: object, endpoint_id: int) -> bool:
    """Return True only when NetBox proves that the plugin has no claim action route.

    Without the route, NetBox's URL router answers with its HTML "Page Not Found"
    page. A plugin that has the POST-only action answers 405, 403, or a JSON 404
    for a missing endpoint. Every other answer, and every failure, keeps the atomic
    contract path.
    """
    try:
        probe = await rest_probe_async(nb, f"{ENDPOINT_PATH}{endpoint_id}/{CLAIM_ACTION}/")
    except Exception as error:  # noqa: BLE001 - any probe failure keeps the contract path
        logger.info(
            "Cluster claim capability probe for endpoint %s failed (%s); using the claim action.",
            endpoint_id,
            type(error).__name__,
        )
        return False
    return probe.status == 404 and probe.content_type.strip().lower().startswith("text/html")


TrackingLink = tuple[int | None, int | None, object]


def _tracking_links(rows: list[object]) -> list[TrackingLink]:
    """Return (endpoint ID, linked native cluster ID, name) for every tracking row."""
    links: list[TrackingLink] = []
    for row in rows:
        mapped = to_mapping(row)
        links.append(
            (
                record_id(mapped.get("endpoint")),
                record_id(mapped.get("netbox_cluster")),
                mapped.get("name"),
            )
        )
    return links


def _cluster_owners(links: list[TrackingLink]) -> dict[int, set[int | None]]:
    """Map each linked native cluster ID to the endpoint IDs whose rows link it."""
    owners: dict[int, set[int | None]] = {}
    for endpoint, cluster, _name in links:
        if cluster is not None:
            owners.setdefault(cluster, set()).add(endpoint)
    return owners


def _tracked_cluster_id(links: list[TrackingLink], *, endpoint_id: int, cluster_name: str) -> int:
    """Apply the netbox-proxbox 0.0.31rc2 binding backfill rule to one endpoint.

    The endpoint needs exactly one tracking row with this exact name, and that row
    must link a native cluster. Every linked row of the endpoint must link the same
    cluster, and no row of another endpoint may link it. Server-side filters are
    not trusted: callers pass every tracking row, and all comparisons happen here.
    """
    owners = _cluster_owners(links)
    named = [
        cluster
        for endpoint, cluster, name in links
        if (endpoint, name) == (endpoint_id, cluster_name)
    ]
    linked = {cluster for cluster, endpoints in owners.items() if endpoint_id in endpoints}
    cluster_id = named[0] if len(named) == 1 else None
    # owners[cluster_id] is read only after cluster_id is proven to be the single
    # cluster that this endpoint links, so the key always exists.
    if cluster_id is None or linked != {cluster_id} or owners[cluster_id] != {endpoint_id}:
        raise ClusterBindingError(_LEGACY_ADOPTION_REFUSED)
    return cluster_id


_PAGE_LIMIT = 1000
AUTH_CHECK_PATH = "/api/authentication-check/"
USERS_PATH = "/api/users/users/"


async def _fresh_json(nb: object, path: str, query: dict[str, object] | None = None) -> object:
    """Read current NetBox state for the adoption path; any failure refuses adoption."""
    try:
        return await rest_get_fresh_json(nb, path, query=query)
    except Exception as error:  # noqa: BLE001 - every read failure refuses adoption
        raise ClusterBindingError(_LEGACY_ADOPTION_REFUSED) from error


async def _fresh_page(
    nb: object, path: str, query: dict[str, object] | None = None
) -> list[object]:
    """Return every row of one complete, uncached list page; anything partial refuses."""
    page = to_mapping(await _fresh_json(nb, path, {"limit": _PAGE_LIMIT, **(query or {})}))
    rows = page.get("results")
    if not isinstance(rows, list) or page.get("next") or page.get("count") != len(rows):
        raise ClusterBindingError(_LEGACY_ADOPTION_REFUSED)
    return list(rows)


async def _require_fresh_endpoint(nb: object, source: object, endpoint_id: int) -> None:
    """Re-resolve the endpoint from a fresh, complete list and require the same enabled row.

    A host, port, or fallback IP that changed after the cached resolution, or a
    second endpoint that now shares the transport identity, refuses adoption.
    """
    row = to_mapping(_select_plugin_endpoint(await _fresh_page(nb, ENDPOINT_PATH), source))
    if record_id(row) != endpoint_id or row.get("enabled") is not True:
        raise ClusterBindingError(_LEGACY_ADOPTION_REFUSED)


def _page_ids(rows: list[object]) -> list[int | None]:
    return [record_id(row) for row in rows]


async def _require_unrestricted_visibility(nb: object) -> None:
    """Require a superuser token, so the tracking rows read next are the complete set.

    The users API does not return ``is_superuser``, so the filter is checked from
    both sides: the user must match ``is_superuser=true`` and must not match
    ``is_superuser=false``. A server that ignores the filter fails the second test.
    """
    user_id = to_mapping(await _fresh_json(nb, AUTH_CHECK_PATH)).get("id")
    if type(user_id) is not int or user_id <= 0:
        raise ClusterBindingError(_LEGACY_ADOPTION_REFUSED)
    superuser = await _fresh_page(nb, USERS_PATH, {"id": user_id, "is_superuser": "true"})
    regular = await _fresh_page(nb, USERS_PATH, {"id": user_id, "is_superuser": "false"})
    if _page_ids(superuser) != [user_id] or regular:
        raise ClusterBindingError(_LEGACY_ADOPTION_REFUSED)


async def _fresh_native_cluster(nb: object, cluster_id: int, cluster_name: str) -> RestRecord:
    """Read the native cluster uncached and rebuild its record from the validated ID."""
    body = to_mapping(await _fresh_json(nb, f"{CLUSTER_PATH}{cluster_id}/"))
    try:
        native = NativeClusterResponse.model_validate(body)
    except ValueError as error:
        raise ClusterBindingError(_LEGACY_ADOPTION_REFUSED) from error
    if native.id != cluster_id or body.get("name") != cluster_name:
        raise ClusterBindingError(_LEGACY_ADOPTION_REFUSED)
    canonical = {**body, "id": native.id, "url": f"{CLUSTER_PATH}{native.id}/"}
    return RestRecord(nb, CLUSTER_PATH, canonical)


async def _adopt_tracked_cluster(
    nb: object, *, source: object, endpoint_id: int, cluster_name: str
) -> ClusterClaim:
    """Adopt, read-only, the native cluster that the tracking rows bind to the endpoint.

    Every read is fresh. A superuser token is required so that the exclusivity
    check sees every tracking row; that complete view is what makes it safe for
    callers to adopt unclaimed devices in the cluster. The claim is also
    read-only, so callers never write the cluster itself.
    """
    await _require_unrestricted_visibility(nb)
    await _require_fresh_endpoint(nb, source, endpoint_id)
    cluster_id = _tracked_cluster_id(
        _tracking_links(await _fresh_page(nb, TRACKING_PATH)),
        endpoint_id=endpoint_id,
        cluster_name=cluster_name,
    )
    record = await _fresh_native_cluster(nb, cluster_id, cluster_name)
    logger.warning(
        "netbox-proxbox has no sync-cluster action; adopted native cluster %s for endpoint %s "
        "read-only through its existing tracking row. Upgrade netbox-proxbox to 0.0.31 or later.",
        cluster_id,
        endpoint_id,
    )
    return ClusterClaim(record, created=False, changed=False, read_only=True)


async def claim_endpoint_cluster(
    nb: object,
    *,
    source: object,
    cluster_name: str,
    mode: str,
    cluster_type_id: int | None,
    site_id: int | None = None,
    tenant_id: int | None = None,
) -> ClusterClaim:
    """Atomically resolve/create the core cluster before any native metadata write.

    A plugin without the claim action gets read-only adoption of the cluster that
    its tracking rows already bind to the endpoint, and nothing else.
    """
    endpoint_id = await resolve_plugin_endpoint_id(nb, source)
    payload: dict[str, object] = {
        "name": cluster_name,
        "mode": mode,
        "cluster_type": cluster_type_id,
        "site": site_id,
        "tenant": tenant_id,
    }
    cluster_id = _value(source, "id")
    if isinstance(cluster_id, str):
        payload["cluster_id"] = cluster_id
    request = ClusterClaimRequest.model_validate(payload)
    if await _claim_action_absent(nb, endpoint_id):
        return await _adopt_tracked_cluster(
            nb, source=source, endpoint_id=endpoint_id, cluster_name=request.name
        )
    response = await rest_create_async(
        nb, f"{ENDPOINT_PATH}{endpoint_id}/{CLAIM_ACTION}/", request.model_dump(exclude_none=True)
    )
    validated = ClusterClaimResponse.model_validate(to_mapping(response))
    native = validated.cluster.model_dump()
    native["url"] = f"{CLUSTER_PATH}{validated.cluster.id}/"
    return ClusterClaim(RestRecord(nb, CLUSTER_PATH, native), validated.created, validated.changed)
