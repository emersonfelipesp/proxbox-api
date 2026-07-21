"""Tiered ZFS storage retrieval for Proxmox-backed inventory consumers."""

from __future__ import annotations

import re
from collections.abc import Sequence

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.schemas.zfs import (
    ZfsDataSourceAttempt,
    ZfsPoolDetail,
    ZfsPoolDetailResponse,
    ZfsPoolsResponse,
    ZfsPoolSummary,
    ZfsTierName,
    ZfsVdevNode,
)
from proxbox_api.services.proxmox_helpers import get_node_zfs_pool_detail, get_node_zfs_pools
from proxbox_api.session.proxmox import ProxmoxSession

_DEFAULT_TIER_ORDER: tuple[ZfsTierName, ...] = ("proxmox_api", "influxdb", "ssh_cli")
_MAX_VDEV_TREE_DEPTH = 64
_CREDENTIAL_URL_PATTERN = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<userinfo>[^/\s@]*:[^/\s@]*@)",
    re.IGNORECASE,
)
_AUTHORIZATION_VALUE_PATTERN = re.compile(
    r"\b(authorization\s*[:=]\s*)(?:(bearer|basic|token)\s+)?([^\s&;,}]+)",
    re.IGNORECASE,
)
_AUTH_SCHEME_TOKEN_PATTERN = re.compile(
    r"(?<![-\w])((?:bearer|basic|token)\s+)([^\s&;,}]+)",
    re.IGNORECASE,
)
_SENSITIVE_JSON_PATTERN = re.compile(
    r"(?i)([\"']?(?:password|passwd|pass|token(?:_value)?|api[_-]?key|"
    r"csrfpreventiontoken|authorization|secret|client[_-]?secret|ticket)[\"']?\s*:\s*)"
    r"([\"'])(.*?)(\2)"
)
_SENSITIVE_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pass|token(?:_value)?|api[_-]?key|csrfpreventiontoken|"
    r"secret|client[_-]?secret|ticket)\b(\s*[=:]\s*)([^\s&;,}]+)"
)


def _safe_error_detail(error: BaseException) -> str:
    detail = str(error) or error.__class__.__name__
    detail = _CREDENTIAL_URL_PATTERN.sub(r"\g<scheme>[REDACTED]@", detail)
    detail = _AUTHORIZATION_VALUE_PATTERN.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)} [REDACTED]"
            if match.group(2)
            else f"{match.group(1)}[REDACTED]"
        ),
        detail,
    )
    detail = _AUTH_SCHEME_TOKEN_PATTERN.sub(r"\1[REDACTED]", detail)
    detail = _SENSITIVE_JSON_PATTERN.sub(r"\1\2[REDACTED]\4", detail)
    return _SENSITIVE_KEY_VALUE_PATTERN.sub(r"\1\2[REDACTED]", detail)


def _dump_model_or_mapping(value: object) -> dict[str, object]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python", by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        return dict(value)
    return {}


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _coerce_float(value: object) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _coerce_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse_vdev_tree(items: object, *, depth: int = 0) -> list[ZfsVdevNode]:
    if not isinstance(items, list):
        return []
    if depth > _MAX_VDEV_TREE_DEPTH:
        raise ProxboxException(
            message="ZFS vdev tree exceeds maximum depth",
            detail={"max_depth": _MAX_VDEV_TREE_DEPTH},
            http_status_code=502,
        )

    nodes: list[ZfsVdevNode] = []
    for item in items:
        row = item if isinstance(item, dict) else {}
        children = row.get("children")
        if isinstance(children, list) and children and depth >= _MAX_VDEV_TREE_DEPTH:
            raise ProxboxException(
                message="ZFS vdev tree exceeds maximum depth",
                detail={"max_depth": _MAX_VDEV_TREE_DEPTH},
                http_status_code=502,
            )
        nodes.append(
            ZfsVdevNode(
                name=_coerce_str(row.get("name")),
                state=_coerce_str(row.get("state")),
                read=_coerce_int(row.get("read")),
                write=_coerce_int(row.get("write")),
                cksum=_coerce_int(row.get("cksum")),
                msg=_coerce_str(row.get("msg")),
                children=_parse_vdev_tree(children, depth=depth + 1),
            )
        )
    return nodes


def _cluster_name_for_session(session: ProxmoxSession) -> str:
    cluster_name = getattr(session, "cluster_name", None)
    if cluster_name:
        return str(cluster_name)

    for item in getattr(session, "cluster_status", None) or []:
        row = item if isinstance(item, dict) else {}
        if row.get("type") == "cluster" and isinstance(row.get("name"), str):
            return str(row["name"])

    session_name = getattr(session, "name", None)
    if session_name:
        return str(session_name)

    return "unknown"


def _real_cluster_name_for_dedupe(session: ProxmoxSession) -> str | None:
    cluster_name = getattr(session, "cluster_name", None)
    if cluster_name:
        return str(cluster_name)

    for item in getattr(session, "cluster_status", None) or []:
        row = item if isinstance(item, dict) else {}
        if row.get("type") == "cluster" and isinstance(row.get("name"), str):
            return str(row["name"])

    return None


def _endpoint_identity_for_dedupe(session: ProxmoxSession) -> str:
    for attr in ("db_endpoint_id", "endpoint_id", "id"):
        value = getattr(session, attr, None)
        if value is not None:
            return f"{attr}:{value}"

    for attr in ("base_url", "url", "host"):
        value = getattr(session, attr, None)
        if value:
            return f"{attr}:{value}"

    host = getattr(session, "domain", None) or getattr(session, "ip_address", None)
    if host:
        scheme = "https" if getattr(session, "ssl", True) else "http"
        port = getattr(session, "http_port", None) or 8006
        return f"endpoint:{scheme}://{host}:{port}"

    return f"unknown-endpoint:{id(session)}"


def _session_dedupe_key(session: ProxmoxSession) -> str:
    cluster_name = _real_cluster_name_for_dedupe(session)
    if cluster_name is not None:
        return f"cluster:{cluster_name}"
    return _endpoint_identity_for_dedupe(session)


def _dedupe_sessions_by_cluster(sessions: Sequence[ProxmoxSession]) -> list[ProxmoxSession]:
    deduped: list[ProxmoxSession] = []
    seen: set[str] = set()
    for session in sessions:
        key = _session_dedupe_key(session)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(session)
    return deduped


async def _node_names_for_session(session: ProxmoxSession, requested_node: str | None) -> list[str]:
    if requested_node:
        return [requested_node]

    names: list[str] = []
    for item in getattr(session, "cluster_status", None) or []:
        row = item if isinstance(item, dict) else {}
        if row.get("type") == "node" and isinstance(row.get("name"), str):
            names.append(str(row["name"]))

    if names:
        return list(dict.fromkeys(names))

    sdk = getattr(session, "session", None)
    if sdk is None:
        return []

    rows = await resolve_async(sdk.nodes.get())
    if not isinstance(rows, list):
        return []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("node"), str):
            names.append(str(row["node"]))
    return list(dict.fromkeys(names))


async def _safe_node_names_for_fetch(
    *,
    session: ProxmoxSession,
    requested_node: str | None,
    cluster_name: str,
    operation: str,
    errors: list[str],
    pool_name: str | None = None,
) -> list[str]:
    try:
        return await _node_names_for_session(session, requested_node)
    except Exception as error:  # noqa: BLE001
        safe_error = _safe_error_detail(error)
        pool_context = f" pool={pool_name}" if pool_name is not None else ""
        logger.warning(
            "Unable to discover Proxmox nodes for ZFS %s cluster=%s%s: %s",
            operation,
            cluster_name,
            pool_context,
            safe_error,
        )
        errors.append(f"{cluster_name}: node discovery: {safe_error}")
        return []


def _summary_from_api(
    *,
    cluster_name: str,
    node: str,
    row: object,
) -> ZfsPoolSummary | None:
    data = _dump_model_or_mapping(row)
    name = _coerce_str(data.get("name"))
    if not name:
        return None
    return ZfsPoolSummary(
        cluster_name=cluster_name,
        node=node,
        name=name,
        size=_coerce_int(data.get("size")),
        alloc=_coerce_int(data.get("alloc")),
        free=_coerce_int(data.get("free")),
        frag=_coerce_int(data.get("frag")),
        dedup=_coerce_float(data.get("dedup")),
        health=_coerce_str(data.get("health")),
        source="proxmox_api",
    )


def _detail_from_api(
    *,
    summary: ZfsPoolSummary | None,
    cluster_name: str,
    node: str,
    name: str,
    row: object,
) -> ZfsPoolDetail:
    data = _dump_model_or_mapping(row)
    return ZfsPoolDetail(
        cluster_name=cluster_name,
        node=node,
        name=_coerce_str(data.get("name")) or name,
        size=summary.size if summary else None,
        alloc=summary.alloc if summary else None,
        free=summary.free if summary else None,
        frag=summary.frag if summary else None,
        dedup=summary.dedup if summary else None,
        health=summary.health if summary else None,
        source="proxmox_api",
        state=_coerce_str(data.get("state")),
        status=_coerce_str(data.get("status")),
        action=_coerce_str(data.get("action")),
        scan=_coerce_str(data.get("scan")),
        errors=_coerce_str(data.get("errors")),
        children=_parse_vdev_tree(data.get("children")),
    )


async def _fetch_pools_from_proxmox_api(
    sessions: Sequence[ProxmoxSession],
    *,
    node: str | None,
) -> list[ZfsPoolSummary]:
    pools: list[ZfsPoolSummary] = []
    errors: list[str] = []

    for session in _dedupe_sessions_by_cluster(sessions):
        cluster_name = _cluster_name_for_session(session)
        node_names = await _safe_node_names_for_fetch(
            session=session,
            requested_node=node,
            cluster_name=cluster_name,
            operation="pool list",
            errors=errors,
        )

        for node_name in node_names:
            try:
                rows = await get_node_zfs_pools(session, node_name)
            except Exception as error:  # noqa: BLE001
                safe_error = _safe_error_detail(error)
                logger.warning(
                    "Unable to fetch ZFS pool list from Proxmox API for cluster=%s node=%s: %s",
                    cluster_name,
                    node_name,
                    safe_error,
                )
                errors.append(f"{cluster_name}/{node_name}: {safe_error}")
                continue
            for row in rows:
                summary = _summary_from_api(cluster_name=cluster_name, node=node_name, row=row)
                if summary is not None:
                    pools.append(summary)

    if not pools and errors:
        raise ProxboxException(
            message="Unable to fetch ZFS pool summaries from Proxmox API",
            detail={"errors": errors[:10]},
            http_status_code=502,
        )

    return pools


async def _fetch_pool_details_from_proxmox_api(
    sessions: Sequence[ProxmoxSession],
    *,
    name: str,
    node: str | None,
) -> list[ZfsPoolDetail]:
    details: list[ZfsPoolDetail] = []
    errors: list[str] = []

    for session in _dedupe_sessions_by_cluster(sessions):
        cluster_name = _cluster_name_for_session(session)
        node_names = await _safe_node_names_for_fetch(
            session=session,
            requested_node=node,
            cluster_name=cluster_name,
            operation="pool detail",
            errors=errors,
            pool_name=name,
        )

        for node_name in node_names:
            summaries_by_name: dict[str, ZfsPoolSummary] = {}
            try:
                rows = await get_node_zfs_pools(session, node_name)
            except Exception as error:  # noqa: BLE001
                safe_error = _safe_error_detail(error)
                logger.warning(
                    "Unable to fetch ZFS pool summaries before detail for cluster=%s node=%s: %s",
                    cluster_name,
                    node_name,
                    safe_error,
                )
                errors.append(f"{cluster_name}/{node_name}: {safe_error}")
            else:
                for row in rows:
                    summary = _summary_from_api(cluster_name=cluster_name, node=node_name, row=row)
                    if summary is not None:
                        summaries_by_name[summary.name] = summary
                if name not in summaries_by_name:
                    continue

            try:
                detail = await get_node_zfs_pool_detail(session, node_name, name)
            except Exception as error:  # noqa: BLE001
                safe_error = _safe_error_detail(error)
                logger.warning(
                    "Unable to fetch ZFS pool detail from Proxmox API for cluster=%s node=%s pool=%s: %s",
                    cluster_name,
                    node_name,
                    name,
                    safe_error,
                )
                errors.append(f"{cluster_name}/{node_name}/{name}: {safe_error}")
                continue

            details.append(
                _detail_from_api(
                    summary=summaries_by_name.get(name),
                    cluster_name=cluster_name,
                    node=node_name,
                    name=name,
                    row=detail,
                )
            )

    if not details and errors:
        raise ProxboxException(
            message="Unable to fetch ZFS pool detail from Proxmox API",
            detail={"errors": errors[:10], "pool": name},
            http_status_code=502,
        )

    return details


async def _fetch_pools_from_influxdb() -> list[ZfsPoolSummary]:
    logger.debug("ZFS InfluxDB tier skipped: no InfluxDB connector is configured")
    return []


async def _fetch_pool_details_from_influxdb(*, name: str) -> list[ZfsPoolDetail]:
    logger.debug("ZFS InfluxDB detail tier skipped for pool=%s: no connector is configured", name)
    return []


async def _fetch_pools_from_ssh_cli() -> list[ZfsPoolSummary]:
    logger.debug("ZFS SSH CLI tier skipped: JSON-native SSH collection is not implemented")
    return []


async def _fetch_pool_details_from_ssh_cli(*, name: str) -> list[ZfsPoolDetail]:
    logger.debug("ZFS SSH CLI detail tier skipped for pool=%s: not implemented", name)
    return []


def _normalize_tier_order(tiers: Sequence[ZfsTierName] | None) -> tuple[ZfsTierName, ...]:
    if not tiers:
        return _DEFAULT_TIER_ORDER
    normalized: list[ZfsTierName] = []
    for tier in tiers:
        if tier not in _DEFAULT_TIER_ORDER:
            raise ProxboxException(
                message="Invalid ZFS data source tier",
                detail=f"tier must be one of {', '.join(_DEFAULT_TIER_ORDER)}",
            )
        if tier not in normalized:
            normalized.append(tier)
    return tuple(normalized)


async def list_zfs_pools(
    sessions: Sequence[ProxmoxSession],
    *,
    node: str | None = None,
    tiers: Sequence[ZfsTierName] | None = None,
) -> ZfsPoolsResponse:
    """List ZFS pools using the tier order Proxmox API -> InfluxDB -> SSH CLI."""
    if not sessions:
        raise ProxboxException(message="No Proxmox sessions available for ZFS pool retrieval")

    attempts: list[ZfsDataSourceAttempt] = []
    for tier in _normalize_tier_order(tiers):
        try:
            if tier == "proxmox_api":
                pools = await _fetch_pools_from_proxmox_api(sessions, node=node)
            elif tier == "influxdb":
                pools = await _fetch_pools_from_influxdb()
            else:
                pools = await _fetch_pools_from_ssh_cli()
        except ProxboxException as error:
            attempts.append(ZfsDataSourceAttempt(tier=tier, status="failed", reason=str(error)))
            continue

        if pools or tier == "proxmox_api":
            attempts.append(ZfsDataSourceAttempt(tier=tier, status="success"))
            return ZfsPoolsResponse(source=tier, attempted_sources=attempts, pools=pools)

        reason = "not_configured" if tier == "influxdb" else "no_data"
        if tier == "ssh_cli":
            reason = "not_implemented"
        attempts.append(ZfsDataSourceAttempt(tier=tier, status="skipped", reason=reason))

    return ZfsPoolsResponse(source=None, attempted_sources=attempts, pools=[])


async def get_zfs_pool_detail(
    sessions: Sequence[ProxmoxSession],
    *,
    name: str,
    node: str | None = None,
    tiers: Sequence[ZfsTierName] | None = None,
) -> ZfsPoolDetailResponse:
    """Get detailed ZFS pool records using the configured tier order."""
    if not sessions:
        raise ProxboxException(message="No Proxmox sessions available for ZFS pool retrieval")

    attempts: list[ZfsDataSourceAttempt] = []
    for tier in _normalize_tier_order(tiers):
        try:
            if tier == "proxmox_api":
                pools = await _fetch_pool_details_from_proxmox_api(sessions, name=name, node=node)
            elif tier == "influxdb":
                pools = await _fetch_pool_details_from_influxdb(name=name)
            else:
                pools = await _fetch_pool_details_from_ssh_cli(name=name)
        except ProxboxException as error:
            attempts.append(ZfsDataSourceAttempt(tier=tier, status="failed", reason=str(error)))
            continue

        if pools or tier == "proxmox_api":
            attempts.append(ZfsDataSourceAttempt(tier=tier, status="success"))
            return ZfsPoolDetailResponse(source=tier, attempted_sources=attempts, pools=pools)

        reason = "not_configured" if tier == "influxdb" else "no_data"
        if tier == "ssh_cli":
            reason = "not_implemented"
        attempts.append(ZfsDataSourceAttempt(tier=tier, status="skipped", reason=reason))

    return ZfsPoolDetailResponse(source=None, attempted_sources=attempts, pools=[])
