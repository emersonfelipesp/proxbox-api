"""Proxmox VE firewall endpoints.

Surfaces firewall rules, security groups, IP sets, aliases, and options for all
firewall zones: datacenter, node, VM (QEMU + LXC), and VNet SDN (tech-preview).

PVE 8.x vs 9.x compatibility: both versions share the same REST API surface.
The nftables vs iptables backend difference is a runtime flag, not an API
difference. SDK calls that are not implemented on older/newer clusters degrade
gracefully — HTTP 500 responses are caught and return empty lists silently.

Write endpoints are intentionally explicit operator actions. They are gated by
``ProxmoxEndpoint.allow_writes`` and require ``X-Proxbox-Actor`` attribution.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from proxmox_sdk.sdk.exceptions import ResourceException
from pydantic import BaseModel

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.routes.intent.dispatchers.common import get_vm_proxy
from proxbox_api.schemas.firewall import (
    FirewallAliasWrite,
    FirewallIPSetEntryWrite,
    FirewallIPSetWrite,
    FirewallOptionsWrite,
    FirewallRuleWrite,
    FirewallSecurityGroupWrite,
    FirewallVmType,
    FirewallWriteResponse,
)
from proxbox_api.session.proxmox import ProxmoxSession, ProxmoxSessionsDep
from proxbox_api.session.proxmox_providers import _parse_db_endpoint
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

router = APIRouter()


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class FirewallRuleSchema(BaseModel):
    cluster_name: str | None = None
    zone: str | None = None
    node: str | None = None
    vmid: int | None = None
    security_group: str | None = None
    pos: int | None = None
    type: str | None = None
    action: str | None = None
    enable: bool | int | None = None
    macro: str | None = None
    iface: str | None = None
    source: str | None = None
    dest: str | None = None
    proto: str | None = None
    dport: str | None = None
    sport: str | None = None
    log: str | None = None
    icmp_type: str | None = None
    comment: str | None = None
    digest: str | None = None
    status: str = "ok"
    error: str | None = None


class FirewallSecurityGroupSchema(BaseModel):
    cluster_name: str | None = None
    name: str | None = None
    comment: str | None = None
    digest: str | None = None
    rules: list[FirewallRuleSchema] = []
    status: str = "ok"
    error: str | None = None


class FirewallIPSetEntrySchema(BaseModel):
    cidr: str | None = None
    comment: str | None = None
    nomatch: bool | int | None = None


class FirewallIPSetSchema(BaseModel):
    cluster_name: str | None = None
    zone: str | None = None
    node: str | None = None
    vmid: int | None = None
    name: str | None = None
    comment: str | None = None
    digest: str | None = None
    entries: list[FirewallIPSetEntrySchema] = []
    status: str = "ok"
    error: str | None = None


class FirewallAliasSchema(BaseModel):
    cluster_name: str | None = None
    zone: str | None = None
    node: str | None = None
    vmid: int | None = None
    name: str | None = None
    cidr: str | None = None
    comment: str | None = None
    digest: str | None = None
    status: str = "ok"
    error: str | None = None


class FirewallOptionsSchema(BaseModel):
    cluster_name: str | None = None
    zone: str | None = None
    node: str | None = None
    vmid: int | None = None
    enable: bool | int | None = None
    policy_in: str | None = None
    policy_out: str | None = None
    options: dict = {}
    status: str = "ok"
    error: str | None = None


class FirewallSummarySchema(BaseModel):
    cluster_name: str | None = None
    datacenter_rules: list[FirewallRuleSchema] = []
    security_groups: list[FirewallSecurityGroupSchema] = []
    datacenter_ipsets: list[FirewallIPSetSchema] = []
    datacenter_aliases: list[FirewallAliasSchema] = []
    datacenter_options: FirewallOptionsSchema | None = None
    node_rules: list[FirewallRuleSchema] = []
    node_options: list[FirewallOptionsSchema] = []
    vm_rules: list[FirewallRuleSchema] = []
    vm_ipsets: list[FirewallIPSetSchema] = []
    vm_aliases: list[FirewallAliasSchema] = []
    vm_options: list[FirewallOptionsSchema] = []
    status: str = "ok"
    error: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _bool(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _safe_get(coro_or_result) -> list:
    """Await a proxmoxer call, returning [] on any exception."""
    try:
        result = await resolve_async(coro_or_result)
        return result if isinstance(result, list) else ([] if result is None else [result])
    except Exception as exc:
        logger.debug("_safe_get suppressed: %s", exc)
        return []


async def _safe_get_dict(coro_or_result) -> dict:
    """Await a proxmoxer call, returning {} on any exception."""
    try:
        result = await resolve_async(coro_or_result)
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


async def _firewall_gate(
    session: SessionDep,
    endpoint_id: int | None,
) -> JSONResponse | ProxmoxEndpoint:
    """Resolve the target endpoint and enforce the firewall write gate."""
    if endpoint_id is None:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "reason": "endpoint_id_required",
                "detail": "Firewall writes require an explicit endpoint_id query parameter.",
            },
        )

    endpoint = await _maybe_await(session.get(ProxmoxEndpoint, endpoint_id))
    if endpoint is None:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "reason": "endpoint_not_found",
                "detail": f"No ProxmoxEndpoint with id={endpoint_id}.",
            },
        )

    if not endpoint.allow_writes:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "reason": "writes_disabled_for_endpoint",
                "detail": (
                    "Firewall writes are disabled on this endpoint. Enable "
                    "ProxmoxEndpoint.allow_writes before pushing firewall changes."
                ),
                "endpoint_id": endpoint.id,
            },
        )

    return endpoint


async def _open_proxmox_session(endpoint: ProxmoxEndpoint) -> ProxmoxSession:
    """Open a Proxmox API session for a DB endpoint."""
    schema = _parse_db_endpoint(endpoint)
    return await ProxmoxSession.create(schema)


async def _close_proxmox_session(proxmox: ProxmoxSession) -> None:
    close_method = getattr(proxmox, "aclose", None)
    if callable(close_method):
        try:
            await close_method()
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            logger.debug("Failed to close firewall write Proxmox session: %s", exc)


def _require_actor(actor: str | None) -> str:
    actor_value = (actor or "").strip()
    if not actor_value:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "actor_required",
                "detail": "Firewall writes require X-Proxbox-Actor.",
            },
        )
    return actor_value


def _task_id(raw: object) -> str | None:
    if isinstance(raw, str) and raw.startswith("UPID:"):
        return raw
    if isinstance(raw, dict):
        value = raw.get("upid") or raw.get("task") or raw.get("data")
        if isinstance(value, str) and value.startswith("UPID:"):
            return value
    return None


def _unsupported_response(
    *,
    endpoint: ProxmoxEndpoint,
    actor: str,
    path: str,
    reason: str,
    detail: str,
) -> FirewallWriteResponse:
    return FirewallWriteResponse(
        status="skipped",
        endpoint_id=endpoint.id,
        cluster_name=endpoint.name,
        actor=actor,
        path=path,
        reason=reason,
        detail=detail,
    )


async def _firewall_write(
    *,
    db: SessionDep,
    endpoint_id: int | None,
    actor: str | None,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    unsupported_reason: str = "firewall_write_not_supported",
    probe_supported: bool = False,
) -> FirewallWriteResponse | JSONResponse:
    """Dispatch one write through proxmox-sdk with gate and 501 handling."""
    actor_value = _require_actor(actor)
    endpoint_or_response = await _firewall_gate(db, endpoint_id)
    if isinstance(endpoint_or_response, JSONResponse):
        return endpoint_or_response
    endpoint = endpoint_or_response

    try:
        proxmox = await _open_proxmox_session(endpoint)
    except Exception as exc:
        logger.warning("Failed to open Proxmox session for firewall write: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "reason": "proxmox_session_unreachable",
                "detail": str(exc),
                "endpoint_id": endpoint.id,
            },
        ) from exc

    try:
        resource = proxmox.session(path)
        try:
            if probe_supported:
                await resolve_async(resource.get())
            write_call = getattr(resource, method)
            raw = await resolve_async(write_call(**(payload or {})))
        except ResourceException as exc:
            if exc.status_code in {404, 501}:
                return _unsupported_response(
                    endpoint=endpoint,
                    actor=actor_value,
                    path=path,
                    reason=unsupported_reason,
                    detail=f"Upstream Proxmox returned HTTP {exc.status_code}.",
                )
            raise

        return FirewallWriteResponse(
            status="deleted" if method == "delete" else "pushed",
            endpoint_id=endpoint.id,
            cluster_name=endpoint.name,
            actor=actor_value,
            path=path,
            proxmox_task_id=_task_id(raw),
            response=raw,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Firewall write failed for endpoint=%s path=%s", endpoint.id, path)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "reason": "proxmox_firewall_write_failed",
                "detail": str(exc),
                "endpoint_id": endpoint.id,
                "path": path,
            },
        ) from exc
    finally:
        await _close_proxmox_session(proxmox)


def _rule_payload(payload: FirewallRuleWrite) -> dict[str, object]:
    return payload.pve_payload()


def _entry_payload(payload: FirewallIPSetEntryWrite) -> dict[str, object]:
    return payload.pve_payload()


_OPTIONS_KNOWN_KEYS: frozenset[str] = frozenset(
    {"enable", "policy_in", "policy_out", "log_ratelimit"}
)


def _rule_from_raw(raw: dict, cluster_name: str, zone: str, **extra) -> FirewallRuleSchema:
    return FirewallRuleSchema(
        cluster_name=cluster_name,
        zone=zone,
        pos=_int(raw.get("pos")),
        type=raw.get("type"),
        action=raw.get("action"),
        enable=_bool(raw.get("enable")),
        macro=raw.get("macro"),
        iface=raw.get("iface"),
        source=raw.get("source"),
        dest=raw.get("dest"),
        proto=raw.get("proto"),
        dport=raw.get("dport"),
        sport=raw.get("sport"),
        log=raw.get("log"),
        icmp_type=raw.get("icmp-type"),
        comment=raw.get("comment"),
        digest=raw.get("digest"),
        **extra,
    )


def _options_from_raw(raw: dict, cluster_name: str, zone: str, **extra) -> FirewallOptionsSchema:
    return FirewallOptionsSchema(
        cluster_name=cluster_name,
        zone=zone,
        enable=_bool(raw.get("enable")),
        policy_in=raw.get("policy_in"),
        policy_out=raw.get("policy_out"),
        options={k: v for k, v in raw.items() if k not in _OPTIONS_KNOWN_KEYS},
        **extra,
    )


# ── Datacenter-level endpoints ────────────────────────────────────────────────


@router.get("/firewall/datacenter/rules", response_model=list[FirewallRuleSchema])
async def datacenter_firewall_rules(pxs: ProxmoxSessionsDep):
    """Retrieve datacenter-level firewall rules from all configured endpoints."""
    results: list[FirewallRuleSchema] = []
    for px in pxs:
        try:
            raw_rules = await _safe_get(px.session.cluster.firewall.rules.get())
            for rule in raw_rules:
                results.append(_rule_from_raw(rule, px.name, "datacenter"))
        except Exception as exc:
            logger.exception("Error fetching datacenter firewall rules for %s", px.name)
            results.append(
                FirewallRuleSchema(
                    cluster_name=px.name, zone="datacenter", status="error", error=str(exc)
                )
            )
    return results


@router.get("/firewall/datacenter/groups", response_model=list[FirewallSecurityGroupSchema])
async def datacenter_firewall_groups(pxs: ProxmoxSessionsDep):
    """Retrieve datacenter firewall security groups (cluster-wide named rule sets)."""
    results: list[FirewallSecurityGroupSchema] = []
    for px in pxs:
        try:
            raw_groups = await _safe_get(px.session.cluster.firewall.groups.get())
            for grp in raw_groups:
                group_name = grp.get("group") or grp.get("name") or ""
                raw_rules = await _safe_get(px.session.cluster.firewall.groups(group_name).get())
                rule_schemas = [
                    _rule_from_raw(r, px.name, "security_group", security_group=group_name)
                    for r in raw_rules
                ]
                results.append(
                    FirewallSecurityGroupSchema(
                        cluster_name=px.name,
                        name=group_name,
                        comment=grp.get("comment"),
                        digest=grp.get("digest"),
                        rules=rule_schemas,
                    )
                )
        except Exception as exc:
            logger.exception("Error fetching datacenter firewall groups for %s", px.name)
            results.append(
                FirewallSecurityGroupSchema(cluster_name=px.name, status="error", error=str(exc))
            )
    return results


@router.get("/firewall/datacenter/ipsets", response_model=list[FirewallIPSetSchema])
async def datacenter_firewall_ipsets(pxs: ProxmoxSessionsDep):
    """Retrieve datacenter-level firewall IP sets and their entries."""
    results: list[FirewallIPSetSchema] = []
    for px in pxs:
        try:
            raw_sets = await _safe_get(px.session.cluster.firewall.ipset.get())
            for ipset in raw_sets:
                set_name = ipset.get("name") or ""
                raw_entries = await _safe_get(px.session.cluster.firewall.ipset(set_name).get())
                entries = [
                    FirewallIPSetEntrySchema(
                        cidr=e.get("cidr"),
                        comment=e.get("comment"),
                        nomatch=_bool(e.get("nomatch")),
                    )
                    for e in raw_entries
                ]
                results.append(
                    FirewallIPSetSchema(
                        cluster_name=px.name,
                        zone="datacenter",
                        name=set_name,
                        comment=ipset.get("comment"),
                        digest=ipset.get("digest"),
                        entries=entries,
                    )
                )
        except Exception as exc:
            logger.exception("Error fetching datacenter firewall IP sets for %s", px.name)
            results.append(
                FirewallIPSetSchema(
                    cluster_name=px.name, zone="datacenter", status="error", error=str(exc)
                )
            )
    return results


@router.get("/firewall/datacenter/aliases", response_model=list[FirewallAliasSchema])
async def datacenter_firewall_aliases(pxs: ProxmoxSessionsDep):
    """Retrieve datacenter-level firewall IP aliases."""
    results: list[FirewallAliasSchema] = []
    for px in pxs:
        try:
            raw_aliases = await _safe_get(px.session.cluster.firewall.aliases.get())
            for alias in raw_aliases:
                results.append(
                    FirewallAliasSchema(
                        cluster_name=px.name,
                        zone="datacenter",
                        name=alias.get("name"),
                        cidr=alias.get("cidr"),
                        comment=alias.get("comment"),
                        digest=alias.get("digest"),
                    )
                )
        except Exception as exc:
            logger.exception("Error fetching datacenter firewall aliases for %s", px.name)
            results.append(
                FirewallAliasSchema(
                    cluster_name=px.name, zone="datacenter", status="error", error=str(exc)
                )
            )
    return results


@router.get("/firewall/datacenter/options", response_model=FirewallOptionsSchema | None)
async def datacenter_firewall_options(pxs: ProxmoxSessionsDep):
    """Retrieve datacenter-level firewall options (returns first endpoint result)."""
    for px in pxs:
        try:
            raw = await _safe_get_dict(px.session.cluster.firewall.options.get())
            if raw:
                return _options_from_raw(raw, px.name, "datacenter")
        except Exception:
            logger.exception("Error fetching datacenter firewall options for %s", px.name)
    return None


# ── Datacenter-level write endpoints ─────────────────────────────────────────


@router.post("/firewall/datacenter/rules", response_model=FirewallWriteResponse)
async def create_datacenter_firewall_rule(
    payload: FirewallRuleWrite,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="post",
        path="cluster/firewall/rules",
        payload=_rule_payload(payload),
    )


@router.put("/firewall/datacenter/rules/{pos}", response_model=FirewallWriteResponse)
async def update_datacenter_firewall_rule(
    pos: int,
    payload: FirewallRuleWrite,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="put",
        path=f"cluster/firewall/rules/{pos}",
        payload=_rule_payload(payload),
    )


@router.delete("/firewall/datacenter/rules/{pos}", response_model=FirewallWriteResponse)
async def delete_datacenter_firewall_rule(
    pos: int,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="delete",
        path=f"cluster/firewall/rules/{pos}",
    )


@router.post("/firewall/datacenter/groups", response_model=FirewallWriteResponse)
async def create_datacenter_firewall_group(
    payload: FirewallSecurityGroupWrite,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    body = payload.pve_payload()
    if not body.get("group"):
        raise HTTPException(status_code=422, detail="group or name is required")
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="post",
        path="cluster/firewall/groups",
        payload=body,
    )


@router.delete("/firewall/datacenter/groups/{group}", response_model=FirewallWriteResponse)
async def delete_datacenter_firewall_group(
    group: str,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="delete",
        path=f"cluster/firewall/groups/{group}",
    )


@router.post("/firewall/datacenter/groups/{group}/rules", response_model=FirewallWriteResponse)
async def create_datacenter_firewall_group_rule(
    group: str,
    payload: FirewallRuleWrite,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="post",
        path=f"cluster/firewall/groups/{group}",
        payload=_rule_payload(payload),
    )


@router.put(
    "/firewall/datacenter/groups/{group}/rules/{pos}",
    response_model=FirewallWriteResponse,
)
async def update_datacenter_firewall_group_rule(
    group: str,
    pos: int,
    payload: FirewallRuleWrite,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="put",
        path=f"cluster/firewall/groups/{group}/{pos}",
        payload=_rule_payload(payload),
    )


@router.delete(
    "/firewall/datacenter/groups/{group}/rules/{pos}",
    response_model=FirewallWriteResponse,
)
async def delete_datacenter_firewall_group_rule(
    group: str,
    pos: int,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="delete",
        path=f"cluster/firewall/groups/{group}/{pos}",
    )


@router.post("/firewall/datacenter/ipsets", response_model=FirewallWriteResponse)
async def create_datacenter_firewall_ipset(
    payload: FirewallIPSetWrite,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="post",
        path="cluster/firewall/ipset",
        payload=payload.pve_payload(),
    )


@router.delete("/firewall/datacenter/ipsets/{name}", response_model=FirewallWriteResponse)
async def delete_datacenter_firewall_ipset(
    name: str,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="delete",
        path=f"cluster/firewall/ipset/{name}",
    )


@router.post(
    "/firewall/datacenter/ipsets/{name}/entries",
    response_model=FirewallWriteResponse,
)
async def create_datacenter_firewall_ipset_entry(
    name: str,
    payload: FirewallIPSetEntryWrite,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="post",
        path=f"cluster/firewall/ipset/{name}",
        payload=_entry_payload(payload),
    )


@router.put(
    "/firewall/datacenter/ipsets/{name}/entries/{cidr:path}",
    response_model=FirewallWriteResponse,
)
async def update_datacenter_firewall_ipset_entry(
    name: str,
    cidr: str,
    payload: FirewallIPSetEntryWrite,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="put",
        path=f"cluster/firewall/ipset/{name}/{cidr}",
        payload=_entry_payload(payload),
    )


@router.delete(
    "/firewall/datacenter/ipsets/{name}/entries/{cidr:path}",
    response_model=FirewallWriteResponse,
)
async def delete_datacenter_firewall_ipset_entry(
    name: str,
    cidr: str,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="delete",
        path=f"cluster/firewall/ipset/{name}/{cidr}",
    )


@router.post("/firewall/datacenter/aliases", response_model=FirewallWriteResponse)
async def create_datacenter_firewall_alias(
    payload: FirewallAliasWrite,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="post",
        path="cluster/firewall/aliases",
        payload=payload.pve_payload(),
    )


@router.put("/firewall/datacenter/aliases/{name}", response_model=FirewallWriteResponse)
async def update_datacenter_firewall_alias(
    name: str,
    payload: FirewallAliasWrite,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="put",
        path=f"cluster/firewall/aliases/{name}",
        payload=payload.pve_payload(exclude={"name"}),
    )


@router.delete("/firewall/datacenter/aliases/{name}", response_model=FirewallWriteResponse)
async def delete_datacenter_firewall_alias(
    name: str,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="delete",
        path=f"cluster/firewall/aliases/{name}",
    )


@router.put("/firewall/datacenter/options", response_model=FirewallWriteResponse)
async def update_datacenter_firewall_options(
    payload: FirewallOptionsWrite,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="put",
        path="cluster/firewall/options",
        payload=payload.pve_payload(),
    )


# ── Node-level endpoints ──────────────────────────────────────────────────────


@router.get("/firewall/nodes/{node}/rules", response_model=list[FirewallRuleSchema])
async def node_firewall_rules(node: str, pxs: ProxmoxSessionsDep):
    """Retrieve host-level firewall rules for a Proxmox node."""
    results: list[FirewallRuleSchema] = []
    for px in pxs:
        try:
            raw_rules = await _safe_get(px.session.nodes(node).firewall.rules.get())
            for rule in raw_rules:
                results.append(_rule_from_raw(rule, px.name, "node", node=node))
        except Exception as exc:
            logger.exception("Error fetching node %s firewall rules for %s", node, px.name)
            results.append(
                FirewallRuleSchema(
                    cluster_name=px.name, zone="node", node=node, status="error", error=str(exc)
                )
            )
    return results


@router.get("/firewall/nodes/{node}/options", response_model=FirewallOptionsSchema | None)
async def node_firewall_options(node: str, pxs: ProxmoxSessionsDep):
    """Retrieve host-level firewall options for a Proxmox node."""
    for px in pxs:
        try:
            raw = await _safe_get_dict(px.session.nodes(node).firewall.options.get())
            if raw:
                return _options_from_raw(raw, px.name, "node", node=node)
        except Exception:
            logger.exception("Error fetching node %s firewall options for %s", node, px.name)
    return None


# ── Node-level write endpoints ───────────────────────────────────────────────


@router.post("/firewall/nodes/{node}/rules", response_model=FirewallWriteResponse)
async def create_node_firewall_rule(
    node: str,
    payload: FirewallRuleWrite,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="post",
        path=f"nodes/{node}/firewall/rules",
        payload=_rule_payload(payload),
    )


@router.put("/firewall/nodes/{node}/rules/{pos}", response_model=FirewallWriteResponse)
async def update_node_firewall_rule(
    node: str,
    pos: int,
    payload: FirewallRuleWrite,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="put",
        path=f"nodes/{node}/firewall/rules/{pos}",
        payload=_rule_payload(payload),
    )


@router.delete("/firewall/nodes/{node}/rules/{pos}", response_model=FirewallWriteResponse)
async def delete_node_firewall_rule(
    node: str,
    pos: int,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="delete",
        path=f"nodes/{node}/firewall/rules/{pos}",
    )


@router.put("/firewall/nodes/{node}/options", response_model=FirewallWriteResponse)
async def update_node_firewall_options(
    node: str,
    payload: FirewallOptionsWrite,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="put",
        path=f"nodes/{node}/firewall/options",
        payload=payload.pve_payload(),
    )


# ── VM-level endpoints ────────────────────────────────────────────────────────


@router.get("/firewall/vms/{vmid}/rules", response_model=list[FirewallRuleSchema])
async def vm_firewall_rules(vmid: int, node: str, pxs: ProxmoxSessionsDep, vm_type: str = "qemu"):
    """Retrieve VM/CT-level firewall rules.

    Query params:
    - `node`: Proxmox node name where the VM/CT lives (required)
    - `vm_type`: `qemu` (default) or `lxc`
    """
    results: list[FirewallRuleSchema] = []
    zone = "vm_qemu" if vm_type == "qemu" else "vm_lxc"
    for px in pxs:
        try:
            vm_proxy = get_vm_proxy(px, node, vmid, vm_type)
            raw_rules = await _safe_get(vm_proxy.firewall.rules.get())
            for rule in raw_rules:
                results.append(_rule_from_raw(rule, px.name, zone, node=node, vmid=vmid))
        except Exception as exc:
            logger.exception("Error fetching VM %s firewall rules for %s", vmid, px.name)
            results.append(
                FirewallRuleSchema(
                    cluster_name=px.name,
                    zone=zone,
                    node=node,
                    vmid=vmid,
                    status="error",
                    error=str(exc),
                )
            )
    return results


@router.get("/firewall/vms/{vmid}/ipsets", response_model=list[FirewallIPSetSchema])
async def vm_firewall_ipsets(vmid: int, node: str, pxs: ProxmoxSessionsDep, vm_type: str = "qemu"):
    """Retrieve VM/CT firewall IP sets."""
    results: list[FirewallIPSetSchema] = []
    zone = "vm_qemu" if vm_type == "qemu" else "vm_lxc"
    for px in pxs:
        try:
            vm_proxy = get_vm_proxy(px, node, vmid, vm_type)
            raw_sets = await _safe_get(vm_proxy.firewall.ipset.get())
            for ipset in raw_sets:
                set_name = ipset.get("name") or ""
                raw_entries = await _safe_get(vm_proxy.firewall.ipset(set_name).get())
                entries = [
                    FirewallIPSetEntrySchema(
                        cidr=e.get("cidr"),
                        comment=e.get("comment"),
                        nomatch=_bool(e.get("nomatch")),
                    )
                    for e in raw_entries
                ]
                results.append(
                    FirewallIPSetSchema(
                        cluster_name=px.name,
                        zone=zone,
                        node=node,
                        vmid=vmid,
                        name=set_name,
                        comment=ipset.get("comment"),
                        entries=entries,
                    )
                )
        except Exception as exc:
            logger.exception("Error fetching VM %s firewall IP sets for %s", vmid, px.name)
            results.append(
                FirewallIPSetSchema(
                    cluster_name=px.name,
                    zone=zone,
                    node=node,
                    vmid=vmid,
                    status="error",
                    error=str(exc),
                )
            )
    return results


@router.get("/firewall/vms/{vmid}/aliases", response_model=list[FirewallAliasSchema])
async def vm_firewall_aliases(vmid: int, node: str, pxs: ProxmoxSessionsDep, vm_type: str = "qemu"):
    """Retrieve VM/CT firewall IP aliases."""
    results: list[FirewallAliasSchema] = []
    zone = "vm_qemu" if vm_type == "qemu" else "vm_lxc"
    for px in pxs:
        try:
            vm_proxy = get_vm_proxy(px, node, vmid, vm_type)
            raw_aliases = await _safe_get(vm_proxy.firewall.aliases.get())
            for alias in raw_aliases:
                results.append(
                    FirewallAliasSchema(
                        cluster_name=px.name,
                        zone=zone,
                        node=node,
                        vmid=vmid,
                        name=alias.get("name"),
                        cidr=alias.get("cidr"),
                        comment=alias.get("comment"),
                    )
                )
        except Exception as exc:
            logger.exception("Error fetching VM %s firewall aliases for %s", vmid, px.name)
            results.append(
                FirewallAliasSchema(
                    cluster_name=px.name,
                    zone=zone,
                    node=node,
                    vmid=vmid,
                    status="error",
                    error=str(exc),
                )
            )
    return results


@router.get("/firewall/vms/{vmid}/options", response_model=FirewallOptionsSchema | None)
async def vm_firewall_options(vmid: int, node: str, pxs: ProxmoxSessionsDep, vm_type: str = "qemu"):
    """Retrieve VM/CT firewall options."""
    zone = "vm_qemu" if vm_type == "qemu" else "vm_lxc"
    for px in pxs:
        try:
            vm_proxy = get_vm_proxy(px, node, vmid, vm_type)
            raw = await _safe_get_dict(vm_proxy.firewall.options.get())
            if raw:
                return _options_from_raw(raw, px.name, zone, node=node, vmid=vmid)
        except Exception:
            logger.exception("Error fetching VM %s firewall options for %s", vmid, px.name)
    return None


def _vm_firewall_base(node: str, vmid: int, vm_type: FirewallVmType) -> str:
    return f"nodes/{node}/{vm_type}/{vmid}/firewall"


# ── VM-level write endpoints ─────────────────────────────────────────────────


@router.post("/firewall/vms/{vmid}/rules", response_model=FirewallWriteResponse)
async def create_vm_firewall_rule(
    vmid: int,
    node: str,
    payload: FirewallRuleWrite,
    db: SessionDep,
    vm_type: FirewallVmType = Query(default="qemu"),
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="post",
        path=f"{_vm_firewall_base(node, vmid, vm_type)}/rules",
        payload=_rule_payload(payload),
    )


@router.put("/firewall/vms/{vmid}/rules/{pos}", response_model=FirewallWriteResponse)
async def update_vm_firewall_rule(
    vmid: int,
    pos: int,
    node: str,
    payload: FirewallRuleWrite,
    db: SessionDep,
    vm_type: FirewallVmType = Query(default="qemu"),
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="put",
        path=f"{_vm_firewall_base(node, vmid, vm_type)}/rules/{pos}",
        payload=_rule_payload(payload),
    )


@router.delete("/firewall/vms/{vmid}/rules/{pos}", response_model=FirewallWriteResponse)
async def delete_vm_firewall_rule(
    vmid: int,
    pos: int,
    node: str,
    db: SessionDep,
    vm_type: FirewallVmType = Query(default="qemu"),
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="delete",
        path=f"{_vm_firewall_base(node, vmid, vm_type)}/rules/{pos}",
    )


@router.post("/firewall/vms/{vmid}/ipsets", response_model=FirewallWriteResponse)
async def create_vm_firewall_ipset(
    vmid: int,
    node: str,
    payload: FirewallIPSetWrite,
    db: SessionDep,
    vm_type: FirewallVmType = Query(default="qemu"),
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="post",
        path=f"{_vm_firewall_base(node, vmid, vm_type)}/ipset",
        payload=payload.pve_payload(),
    )


@router.delete("/firewall/vms/{vmid}/ipsets/{name}", response_model=FirewallWriteResponse)
async def delete_vm_firewall_ipset(
    vmid: int,
    name: str,
    node: str,
    db: SessionDep,
    vm_type: FirewallVmType = Query(default="qemu"),
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="delete",
        path=f"{_vm_firewall_base(node, vmid, vm_type)}/ipset/{name}",
    )


@router.post("/firewall/vms/{vmid}/ipsets/{name}/entries", response_model=FirewallWriteResponse)
async def create_vm_firewall_ipset_entry(
    vmid: int,
    name: str,
    node: str,
    payload: FirewallIPSetEntryWrite,
    db: SessionDep,
    vm_type: FirewallVmType = Query(default="qemu"),
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="post",
        path=f"{_vm_firewall_base(node, vmid, vm_type)}/ipset/{name}",
        payload=_entry_payload(payload),
    )


@router.put(
    "/firewall/vms/{vmid}/ipsets/{name}/entries/{cidr:path}",
    response_model=FirewallWriteResponse,
)
async def update_vm_firewall_ipset_entry(
    vmid: int,
    name: str,
    cidr: str,
    node: str,
    payload: FirewallIPSetEntryWrite,
    db: SessionDep,
    vm_type: FirewallVmType = Query(default="qemu"),
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="put",
        path=f"{_vm_firewall_base(node, vmid, vm_type)}/ipset/{name}/{cidr}",
        payload=_entry_payload(payload),
    )


@router.delete(
    "/firewall/vms/{vmid}/ipsets/{name}/entries/{cidr:path}",
    response_model=FirewallWriteResponse,
)
async def delete_vm_firewall_ipset_entry(
    vmid: int,
    name: str,
    cidr: str,
    node: str,
    db: SessionDep,
    vm_type: FirewallVmType = Query(default="qemu"),
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="delete",
        path=f"{_vm_firewall_base(node, vmid, vm_type)}/ipset/{name}/{cidr}",
    )


@router.post("/firewall/vms/{vmid}/aliases", response_model=FirewallWriteResponse)
async def create_vm_firewall_alias(
    vmid: int,
    node: str,
    payload: FirewallAliasWrite,
    db: SessionDep,
    vm_type: FirewallVmType = Query(default="qemu"),
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="post",
        path=f"{_vm_firewall_base(node, vmid, vm_type)}/aliases",
        payload=payload.pve_payload(),
    )


@router.put("/firewall/vms/{vmid}/aliases/{name}", response_model=FirewallWriteResponse)
async def update_vm_firewall_alias(
    vmid: int,
    name: str,
    node: str,
    payload: FirewallAliasWrite,
    db: SessionDep,
    vm_type: FirewallVmType = Query(default="qemu"),
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="put",
        path=f"{_vm_firewall_base(node, vmid, vm_type)}/aliases/{name}",
        payload=payload.pve_payload(exclude={"name"}),
    )


@router.delete("/firewall/vms/{vmid}/aliases/{name}", response_model=FirewallWriteResponse)
async def delete_vm_firewall_alias(
    vmid: int,
    name: str,
    node: str,
    db: SessionDep,
    vm_type: FirewallVmType = Query(default="qemu"),
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="delete",
        path=f"{_vm_firewall_base(node, vmid, vm_type)}/aliases/{name}",
    )


@router.put("/firewall/vms/{vmid}/options", response_model=FirewallWriteResponse)
async def update_vm_firewall_options(
    vmid: int,
    node: str,
    payload: FirewallOptionsWrite,
    db: SessionDep,
    vm_type: FirewallVmType = Query(default="qemu"),
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="put",
        path=f"{_vm_firewall_base(node, vmid, vm_type)}/options",
        payload=payload.pve_payload(),
    )


# ── VNet/SDN firewall write endpoints ────────────────────────────────────────


@router.post("/firewall/vnets/{vnet}/rules", response_model=FirewallWriteResponse)
async def create_vnet_firewall_rule(
    vnet: str,
    payload: FirewallRuleWrite,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    path = f"cluster/sdn/vnets/{vnet}/firewall/rules"
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="post",
        path=path,
        payload=_rule_payload(payload),
        unsupported_reason="vnet_firewall_not_supported",
        probe_supported=True,
    )


@router.put("/firewall/vnets/{vnet}/rules/{pos}", response_model=FirewallWriteResponse)
async def update_vnet_firewall_rule(
    vnet: str,
    pos: int,
    payload: FirewallRuleWrite,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="put",
        path=f"cluster/sdn/vnets/{vnet}/firewall/rules/{pos}",
        payload=_rule_payload(payload),
        unsupported_reason="vnet_firewall_not_supported",
        probe_supported=True,
    )


@router.delete("/firewall/vnets/{vnet}/rules/{pos}", response_model=FirewallWriteResponse)
async def delete_vnet_firewall_rule(
    vnet: str,
    pos: int,
    db: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
):
    return await _firewall_write(
        db=db,
        endpoint_id=endpoint_id,
        actor=actor,
        method="delete",
        path=f"cluster/sdn/vnets/{vnet}/firewall/rules/{pos}",
        unsupported_reason="vnet_firewall_not_supported",
        probe_supported=True,
    )


# ── Aggregated summary endpoint ───────────────────────────────────────────────


@router.get("/firewall/summary", response_model=list[FirewallSummarySchema])
async def firewall_summary(pxs: ProxmoxSessionsDep):
    """Aggregated firewall data for all endpoints — used by the sync stage.

    Returns datacenter rules, security groups (with rules), IP sets (with
    entries), aliases, and options for every configured Proxmox endpoint.
    Node and VM-level data are omitted from this summary to keep the response
    manageable; use the individual endpoints for per-node and per-VM detail.
    """
    results: list[FirewallSummarySchema] = []
    for px in pxs:
        try:
            dc_rules_raw, sg_raw, ipsets_raw, aliases_raw, dc_options_raw = await asyncio.gather(
                _safe_get(px.session.cluster.firewall.rules.get()),
                _safe_get(px.session.cluster.firewall.groups.get()),
                _safe_get(px.session.cluster.firewall.ipset.get()),
                _safe_get(px.session.cluster.firewall.aliases.get()),
                _safe_get_dict(px.session.cluster.firewall.options.get()),
            )

            dc_rules = [_rule_from_raw(r, px.name, "datacenter") for r in dc_rules_raw]

            sg_group_names = [grp.get("group") or grp.get("name") or "" for grp in sg_raw]
            sg_rules_lists = await asyncio.gather(
                *[
                    _safe_get(px.session.cluster.firewall.groups(name).get())
                    for name in sg_group_names
                ]
            )
            security_groups: list[FirewallSecurityGroupSchema] = []
            for grp, group_name, sg_rules in zip(sg_raw, sg_group_names, sg_rules_lists):
                security_groups.append(
                    FirewallSecurityGroupSchema(
                        cluster_name=px.name,
                        name=group_name,
                        comment=grp.get("comment"),
                        digest=grp.get("digest"),
                        rules=[
                            _rule_from_raw(r, px.name, "security_group", security_group=group_name)
                            for r in sg_rules
                        ],
                    )
                )

            ipset_entries_lists = await asyncio.gather(
                *[
                    _safe_get(px.session.cluster.firewall.ipset(ipset.get("name") or "").get())
                    for ipset in ipsets_raw
                ]
            )
            dc_ipsets: list[FirewallIPSetSchema] = []
            for ipset, entries_raw in zip(ipsets_raw, ipset_entries_lists):
                dc_ipsets.append(
                    FirewallIPSetSchema(
                        cluster_name=px.name,
                        zone="datacenter",
                        name=ipset.get("name") or "",
                        comment=ipset.get("comment"),
                        digest=ipset.get("digest"),
                        entries=[
                            FirewallIPSetEntrySchema(
                                cidr=e.get("cidr"),
                                comment=e.get("comment"),
                                nomatch=_bool(e.get("nomatch")),
                            )
                            for e in entries_raw
                        ],
                    )
                )

            dc_aliases = [
                FirewallAliasSchema(
                    cluster_name=px.name,
                    zone="datacenter",
                    name=a.get("name"),
                    cidr=a.get("cidr"),
                    comment=a.get("comment"),
                    digest=a.get("digest"),
                )
                for a in aliases_raw
            ]

            dc_options = (
                _options_from_raw(dc_options_raw, px.name, "datacenter") if dc_options_raw else None
            )

            results.append(
                FirewallSummarySchema(
                    cluster_name=px.name,
                    datacenter_rules=dc_rules,
                    security_groups=security_groups,
                    datacenter_ipsets=dc_ipsets,
                    datacenter_aliases=dc_aliases,
                    datacenter_options=dc_options,
                )
            )
        except Exception as exc:
            logger.exception("Error fetching firewall summary for %s", px.name)
            results.append(
                FirewallSummarySchema(cluster_name=px.name, status="error", error=str(exc))
            )

    return results
