"""Claim endpoint-owned native clusters through the plugin's atomic action."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_interface
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from proxbox_api.netbox_rest import RestRecord, rest_create_async, rest_list_async
from proxbox_api.services.sync.vm_helpers import record_id, to_mapping

ENDPOINT_PATH = "/api/plugins/proxbox/endpoints/proxmox/"
CLUSTER_PATH = "/api/virtualization/clusters/"


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


async def resolve_plugin_endpoint_id(nb: object, source: object) -> int:
    """Resolve exact plugin ownership without conflating independent ID spaces."""
    rows = await rest_list_async(nb, ENDPOINT_PATH)
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
    row = candidates[0]
    if _value(row, "enabled") is False:
        raise ClusterBindingError("The resolved NetBox ProxmoxEndpoint is disabled.")
    return int(record_id(row))


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
    """Atomically resolve/create the core cluster before any native metadata write."""
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
    response = await rest_create_async(
        nb, f"{ENDPOINT_PATH}{endpoint_id}/sync-cluster/", request.model_dump(exclude_none=True)
    )
    validated = ClusterClaimResponse.model_validate(to_mapping(response))
    native = validated.cluster.model_dump()
    native["url"] = f"{CLUSTER_PATH}{validated.cluster.id}/"
    return ClusterClaim(RestRecord(nb, CLUSTER_PATH, native), validated.created, validated.changed)
