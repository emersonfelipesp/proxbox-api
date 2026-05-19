"""Proxmox VE firewall endpoints (read-only).

Surfaces firewall rules, security groups, IP sets, aliases, and options for all
firewall zones: datacenter, node, VM (QEMU + LXC), and VNet SDN (tech-preview).

PVE 8.x vs 9.x compatibility: both versions share the same REST API surface.
The nftables vs iptables backend difference is a runtime flag, not an API
difference. SDK calls that are not implemented on older/newer clusters degrade
gracefully — HTTP 500 responses are caught and return empty lists silently.

All endpoints are read-only by design. Firewall write operations are
explicitly out of scope for this release.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.session.proxmox import ProxmoxSessionsDep

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


_OPTIONS_KNOWN_KEYS: frozenset[str] = frozenset({"enable", "policy_in", "policy_out", "log_ratelimit"})


def _get_vm_proxy(px, node: str, vmid: int, vm_type: str):
    if vm_type == "qemu":
        return px.session.nodes(node).qemu(vmid)
    return px.session.nodes(node).lxc(vmid)


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
            results.append(FirewallRuleSchema(cluster_name=px.name, zone="datacenter", status="error", error=str(exc)))
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
                raw_rules = await _safe_get(
                    px.session.cluster.firewall.groups(group_name).get()
                )
                rule_schemas = [
                    _rule_from_raw(r, px.name, "security_group", security_group=group_name)
                    for r in raw_rules
                ]
                results.append(FirewallSecurityGroupSchema(
                    cluster_name=px.name,
                    name=group_name,
                    comment=grp.get("comment"),
                    digest=grp.get("digest"),
                    rules=rule_schemas,
                ))
        except Exception as exc:
            logger.exception("Error fetching datacenter firewall groups for %s", px.name)
            results.append(FirewallSecurityGroupSchema(cluster_name=px.name, status="error", error=str(exc)))
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
                raw_entries = await _safe_get(
                    px.session.cluster.firewall.ipset(set_name).get()
                )
                entries = [
                    FirewallIPSetEntrySchema(
                        cidr=e.get("cidr"),
                        comment=e.get("comment"),
                        nomatch=_bool(e.get("nomatch")),
                    )
                    for e in raw_entries
                ]
                results.append(FirewallIPSetSchema(
                    cluster_name=px.name,
                    zone="datacenter",
                    name=set_name,
                    comment=ipset.get("comment"),
                    digest=ipset.get("digest"),
                    entries=entries,
                ))
        except Exception as exc:
            logger.exception("Error fetching datacenter firewall IP sets for %s", px.name)
            results.append(FirewallIPSetSchema(cluster_name=px.name, zone="datacenter", status="error", error=str(exc)))
    return results


@router.get("/firewall/datacenter/aliases", response_model=list[FirewallAliasSchema])
async def datacenter_firewall_aliases(pxs: ProxmoxSessionsDep):
    """Retrieve datacenter-level firewall IP aliases."""
    results: list[FirewallAliasSchema] = []
    for px in pxs:
        try:
            raw_aliases = await _safe_get(px.session.cluster.firewall.aliases.get())
            for alias in raw_aliases:
                results.append(FirewallAliasSchema(
                    cluster_name=px.name,
                    zone="datacenter",
                    name=alias.get("name"),
                    cidr=alias.get("cidr"),
                    comment=alias.get("comment"),
                    digest=alias.get("digest"),
                ))
        except Exception as exc:
            logger.exception("Error fetching datacenter firewall aliases for %s", px.name)
            results.append(FirewallAliasSchema(cluster_name=px.name, zone="datacenter", status="error", error=str(exc)))
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
            results.append(FirewallRuleSchema(cluster_name=px.name, zone="node", node=node, status="error", error=str(exc)))
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
            vm_proxy = _get_vm_proxy(px, node, vmid, vm_type)
            raw_rules = await _safe_get(vm_proxy.firewall.rules.get())
            for rule in raw_rules:
                results.append(_rule_from_raw(rule, px.name, zone, node=node, vmid=vmid))
        except Exception as exc:
            logger.exception("Error fetching VM %s firewall rules for %s", vmid, px.name)
            results.append(FirewallRuleSchema(cluster_name=px.name, zone=zone, node=node, vmid=vmid, status="error", error=str(exc)))
    return results


@router.get("/firewall/vms/{vmid}/ipsets", response_model=list[FirewallIPSetSchema])
async def vm_firewall_ipsets(vmid: int, node: str, pxs: ProxmoxSessionsDep, vm_type: str = "qemu"):
    """Retrieve VM/CT firewall IP sets."""
    results: list[FirewallIPSetSchema] = []
    zone = "vm_qemu" if vm_type == "qemu" else "vm_lxc"
    for px in pxs:
        try:
            vm_proxy = _get_vm_proxy(px, node, vmid, vm_type)
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
                results.append(FirewallIPSetSchema(
                    cluster_name=px.name,
                    zone=zone,
                    node=node,
                    vmid=vmid,
                    name=set_name,
                    comment=ipset.get("comment"),
                    entries=entries,
                ))
        except Exception as exc:
            logger.exception("Error fetching VM %s firewall IP sets for %s", vmid, px.name)
            results.append(FirewallIPSetSchema(cluster_name=px.name, zone=zone, node=node, vmid=vmid, status="error", error=str(exc)))
    return results


@router.get("/firewall/vms/{vmid}/aliases", response_model=list[FirewallAliasSchema])
async def vm_firewall_aliases(vmid: int, node: str, pxs: ProxmoxSessionsDep, vm_type: str = "qemu"):
    """Retrieve VM/CT firewall IP aliases."""
    results: list[FirewallAliasSchema] = []
    zone = "vm_qemu" if vm_type == "qemu" else "vm_lxc"
    for px in pxs:
        try:
            vm_proxy = _get_vm_proxy(px, node, vmid, vm_type)
            raw_aliases = await _safe_get(vm_proxy.firewall.aliases.get())
            for alias in raw_aliases:
                results.append(FirewallAliasSchema(
                    cluster_name=px.name,
                    zone=zone,
                    node=node,
                    vmid=vmid,
                    name=alias.get("name"),
                    cidr=alias.get("cidr"),
                    comment=alias.get("comment"),
                ))
        except Exception as exc:
            logger.exception("Error fetching VM %s firewall aliases for %s", vmid, px.name)
            results.append(FirewallAliasSchema(cluster_name=px.name, zone=zone, node=node, vmid=vmid, status="error", error=str(exc)))
    return results


@router.get("/firewall/vms/{vmid}/options", response_model=FirewallOptionsSchema | None)
async def vm_firewall_options(vmid: int, node: str, pxs: ProxmoxSessionsDep, vm_type: str = "qemu"):
    """Retrieve VM/CT firewall options."""
    zone = "vm_qemu" if vm_type == "qemu" else "vm_lxc"
    for px in pxs:
        try:
            vm_proxy = _get_vm_proxy(px, node, vmid, vm_type)
            raw = await _safe_get_dict(vm_proxy.firewall.options.get())
            if raw:
                return _options_from_raw(raw, px.name, zone, node=node, vmid=vmid)
        except Exception:
            logger.exception("Error fetching VM %s firewall options for %s", vmid, px.name)
    return None


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
            sg_rules_lists = await asyncio.gather(*[
                _safe_get(px.session.cluster.firewall.groups(name).get())
                for name in sg_group_names
            ])
            security_groups: list[FirewallSecurityGroupSchema] = []
            for grp, group_name, sg_rules in zip(sg_raw, sg_group_names, sg_rules_lists):
                security_groups.append(FirewallSecurityGroupSchema(
                    cluster_name=px.name,
                    name=group_name,
                    comment=grp.get("comment"),
                    digest=grp.get("digest"),
                    rules=[
                        _rule_from_raw(r, px.name, "security_group", security_group=group_name)
                        for r in sg_rules
                    ],
                ))

            ipset_entries_lists = await asyncio.gather(*[
                _safe_get(px.session.cluster.firewall.ipset(ipset.get("name") or "").get())
                for ipset in ipsets_raw
            ])
            dc_ipsets: list[FirewallIPSetSchema] = []
            for ipset, entries_raw in zip(ipsets_raw, ipset_entries_lists):
                dc_ipsets.append(FirewallIPSetSchema(
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
                ))

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
                _options_from_raw(dc_options_raw, px.name, "datacenter")
                if dc_options_raw
                else None
            )

            results.append(FirewallSummarySchema(
                cluster_name=px.name,
                datacenter_rules=dc_rules,
                security_groups=security_groups,
                datacenter_ipsets=dc_ipsets,
                datacenter_aliases=dc_aliases,
                datacenter_options=dc_options,
            ))
        except Exception as exc:
            logger.exception("Error fetching firewall summary for %s", px.name)
            results.append(FirewallSummarySchema(cluster_name=px.name, status="error", error=str(exc)))

    return results
