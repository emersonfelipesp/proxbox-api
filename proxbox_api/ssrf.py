"""SSRF protection utilities for validating endpoint configurations.

SSRF protection validates that endpoints do not point to restricted IP addresses.
Settings can be configured via NetBox plugin (ProxboxPluginSettings) with caching.

IPs that are already registered in ProxmoxEndpoint or NetBoxEndpoint are automatically allowed.
IPs submitted during endpoint creation/update can be allowed only within that
request so the endpoint's own address passes validation without changing the
process-wide registered-endpoint cache before the database commit succeeds.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from netbox_sdk.facade import Api

logger = logging.getLogger(__name__)

INTERNAL_IP_RANGES = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)

PRIVATE_IP_RANGES = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)

_registered_ips_cache: set[str] = set()
_registered_domains_cache: set[str] = set()


@dataclass(frozen=True, slots=True)
class ResolvedAddress:
    """A validated socket address with IPv6 interface identity preserved."""

    family: int
    address: str
    port: int | None = None
    flowinfo: int = 0
    scope_id: int = 0

    def socket_address(self, port: int) -> tuple[str, int] | tuple[str, int, int, int]:
        resolved_port = self.port if self.port is not None else port
        if self.family == socket.AF_INET6:
            return self.address, resolved_port, self.flowinfo, self.scope_id
        return self.address, resolved_port

    @property
    def httpcore_host(self) -> str:
        """Return a numeric host accepted by httpcore while retaining IPv6 scope."""
        if self.family == socket.AF_INET6 and self.scope_id:
            return f"{self.address}%{self.scope_id}"
        return self.address


def get_registered_endpoints() -> tuple[set[str], set[str]]:
    """Get all IPs and domains from registered endpoints in the database.

    Returns (ips, domains) tuple of sets.
    Cached in memory to avoid repeated DB queries.
    """
    global _registered_ips_cache, _registered_domains_cache

    if _registered_ips_cache or _registered_domains_cache:
        return _registered_ips_cache, _registered_domains_cache

    from sqlmodel import Session, select

    from proxbox_api.database import NetBoxEndpoint, ProxmoxEndpoint, get_engine

    ips: set[str] = set()
    domains: set[str] = set()

    try:
        with Session(get_engine()) as session:
            for endpoint in session.exec(select(ProxmoxEndpoint)).all():
                if endpoint.ip_address:
                    ips.add(endpoint.ip_address.split("/")[0])
                if endpoint.domain:
                    domains.add(endpoint.domain.strip().lower())

            for endpoint in session.exec(select(NetBoxEndpoint)).all():
                if endpoint.ip_address:
                    ips.add(endpoint.ip_address.split("/")[0])
                if endpoint.domain:
                    domains.add(endpoint.domain.strip().lower())

    except Exception as exc:
        # Do not cache a failed load as authoritative: return an empty result so
        # the next call retries the database. Log only the exception type, since
        # the message may embed connection details.
        logger.warning(
            "SSRF: could not load registered endpoints from the database (%s); "
            "registered-endpoint allow-list is unavailable for this check",
            type(exc).__name__,
        )
        return set(), set()

    _registered_ips_cache = ips
    _registered_domains_cache = domains
    return ips, domains


def clear_endpoint_cache() -> None:
    """Clear the registered endpoints cache.

    Call this after creating/updating/deleting endpoints.
    """
    global _registered_ips_cache, _registered_domains_cache
    _registered_ips_cache = set()
    _registered_domains_cache = set()


def pre_allow_endpoint_hosts(*hosts: str, source: str = "endpoint") -> frozenset[str]:
    """Build a request-scoped allow set for endpoint creation or update.

    This function deliberately does not mutate the registered-endpoint cache. The
    caller passes the returned set to :func:`validate_endpoint_host`, and clears
    the global cache only after the endpoint transaction commits successfully.

    Args:
        *hosts: IP address strings or domain names to allow.
        source: Label used in log messages (e.g. "NetBox", "Proxmox").
    """
    allowed: set[str] = set()
    for host in hosts:
        if not host:
            continue
        host = host.strip()
        if not host:
            continue
        normalized = host.lower()
        if normalized not in allowed:
            allowed.add(normalized)
            logger.info(
                "SSRF: created request-scoped allow from %s endpoint configuration",
                source,
            )
    return frozenset(allowed)


def _is_request_allowed(host: str, request_allowed_hosts: Collection[str] | None) -> bool:
    if request_allowed_hosts is None:
        return False
    return host.strip().lower() in request_allowed_hosts


def is_registered_endpoint(host: str) -> bool:
    """Check if host is already registered in ProxmoxEndpoint or NetBoxEndpoint."""
    ips, domains = get_registered_endpoints()
    host_lower = host.strip().lower()

    if host_lower in domains:
        return True

    try:
        ipaddress.ip_address(host)
        return host in ips
    except ValueError:
        pass

    return False


def _normalize_ip(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Unwrap IPv6 forms that embed an IPv4 address so IPv4 range checks apply."""
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return addr.ipv4_mapped
        if addr.sixtofour is not None:
            return addr.sixtofour
        if addr.teredo is not None:
            return addr.teredo[1]
    return addr


def _looks_like_ip_literal(host: str) -> bool:
    """Return True if host is shaped like an IP literal rather than a hostname."""
    return ":" in host or "%" in host or bool(re.fullmatch(r"[0-9.]+", host))


def _ip_allowed_by_settings(
    original: ipaddress.IPv4Address | ipaddress.IPv6Address,
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
    settings: dict,
) -> bool:
    allow_candidates = (original,)
    if isinstance(original, ipaddress.IPv6Address) and original.ipv4_mapped is not None:
        allow_candidates = (original, original.ipv4_mapped)
    allowed_ranges = settings.get("allowed_ip_ranges", [])
    for network in allowed_ranges:
        if any(candidate in network for candidate in allow_candidates):
            return True

    if settings.get("allow_private_ips", False):
        for network in PRIVATE_IP_RANGES:
            if addr in network:
                return True
    return False


def _configured_ip_block(
    ip: str,
    original: ipaddress.IPv4Address | ipaddress.IPv6Address,
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
    candidates: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
    settings: dict,
) -> tuple[bool, str]:
    blocked_ranges = settings.get("blocked_ip_ranges", [])
    for network in blocked_ranges:
        if any(candidate in network for candidate in candidates):
            return True, f"Host '{ip}' is in explicitly blocked range {network}"

    if _ip_allowed_by_settings(original, addr, settings):
        return False, "OK"

    for network in INTERNAL_IP_RANGES:
        if any(candidate in network for candidate in candidates):
            return (
                True,
                f"Host '{ip}' is a reserved/internal IP address. Either add it to ProxmoxEndpoint first, or adjust SSRF settings in ProxboxPluginSettings.",
            )

    if any(not candidate.is_global for candidate in candidates):
        return True, f"Host '{ip}' is a non-global IP address"
    return False, "OK"


def is_ip_blocked(
    ip: str,
    settings: dict | None = None,
) -> tuple[bool, str]:
    """Check if an IP address is blocked based on settings.

    Returns (is_blocked, reason).
    """
    try:
        original = ipaddress.ip_address(ip)
    except ValueError:
        if _looks_like_ip_literal(ip):
            return True, f"Host '{ip}' looks like an IP address but could not be parsed"
        return False, "OK"
    # Evaluate both the address as written and the IPv4 it embeds (mapped, 6to4,
    # Teredo): explicit IPv6 policy keeps applying to the original, and IPv4 policy
    # applies to the embedded address.
    addr = _normalize_ip(original)
    candidates = (original,) if addr == original else (original, addr)

    # Unspecified addresses never identify a routable endpoint. Do this before
    # registered-endpoint and operator-policy exceptions so ``0.0.0.0`` and
    # ``::`` cannot turn into a platform-dependent local connection target.
    if any(candidate.is_unspecified for candidate in candidates):
        return True, f"Host '{ip}' is an unspecified IP address"

    # If IP is already registered, allow it
    if is_registered_endpoint(ip):
        return False, "OK"

    if settings is None:
        settings = {}

    ssrf_enabled = settings.get("ssrf_protection_enabled", True)
    if not ssrf_enabled:
        return False, "OK"

    # Operator allow-lists apply to the address as written and to a true IPv4-mapped
    # form only. 6to4 and Teredo addresses merely embed an IPv4 and reach it through a
    # relay, so an IPv4 allow range must not whitelist them.
    return _configured_ip_block(ip, original, addr, candidates, settings)


def _validate_literal_host(
    host: str,
    settings: dict | None,
    *,
    endpoint_allowed: bool,
) -> tuple[bool, str, tuple[ResolvedAddress, ...]] | None:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return None
    canonical = str(literal).split("%", 1)[0]
    if literal.is_unspecified:
        return False, f"Host '{host}' is an unspecified IP address", ()
    family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
    scope_id = 0
    scope = getattr(literal, "scope_id", None)
    if scope:
        try:
            scope_id = int(scope)
        except ValueError:
            try:
                scope_id = socket.if_nametoindex(scope)
            except OSError:
                return False, f"Host '{host}' has an unknown IPv6 interface scope", ()
    resolved = ResolvedAddress(family, canonical, scope_id=scope_id)
    if endpoint_allowed:
        return True, "OK", (resolved,)
    blocked, reason = is_ip_blocked(canonical, settings)
    return (False, reason, ()) if blocked else (True, "OK", (resolved,))


def _resolve_socket_addresses(
    host: str,
) -> tuple[str | None, tuple[ResolvedAddress, ...]]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        return f"Could not resolve host '{host}': {exc}", ()
    resolved: list[ResolvedAddress] = []
    for info in infos:
        family = info[0]
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            resolved_ip = str(ipaddress.ip_address(str(sockaddr[0])))
        except ValueError:
            return f"Host '{host}' resolved to an invalid IP address", ()
        if family == socket.AF_INET6:
            port, flowinfo, scope_id = sockaddr[1:4]
        else:
            port, flowinfo, scope_id = sockaddr[1], 0, 0
        address = ResolvedAddress(
            family=family,
            address=resolved_ip,
            port=port or None,
            flowinfo=flowinfo,
            scope_id=scope_id,
        )
        if address not in resolved:
            resolved.append(address)
    if not resolved:
        return f"Could not resolve host '{host}'", ()
    return None, tuple(resolved)


def _validate_socket_addresses(
    host: str,
    addresses: tuple[ResolvedAddress, ...],
    settings: dict | None,
    *,
    endpoint_allowed: bool,
) -> tuple[bool, str, tuple[ResolvedAddress, ...]]:
    for resolved in addresses:
        resolved_ip = resolved.address
        if ipaddress.ip_address(resolved_ip).is_unspecified:
            return False, f"Host '{host}' resolves to unspecified address {resolved_ip}", ()
        if endpoint_allowed:
            continue
        blocked, reason = is_ip_blocked(resolved_ip, settings)
        if blocked:
            return False, f"Host '{host}' resolves to {resolved_ip}: {reason}", ()
    return True, "OK", addresses


def resolve_endpoint_host(
    host: str | None,
    settings: dict | None = None,
    *,
    request_allowed_hosts: Collection[str] | None = None,
) -> tuple[bool, str, tuple[ResolvedAddress, ...]]:
    """Validate and return the exact socket addresses an outbound caller may use.

    Registered and request-scoped endpoint names may resolve to private addresses,
    but unspecified addresses are always refused. Callers must connect to one of
    the returned addresses instead of resolving ``host`` again.
    """
    if not host:
        return False, "Host cannot be empty", ()

    host = host.strip()
    request_allowed = _is_request_allowed(host, request_allowed_hosts)
    registered = is_registered_endpoint(host)
    endpoint_allowed = request_allowed or registered

    literal_result = _validate_literal_host(
        host,
        settings,
        endpoint_allowed=endpoint_allowed,
    )
    if literal_result is not None:
        return literal_result

    if _looks_like_ip_literal(host):
        blocked, reason = is_ip_blocked(host, settings)
        return (False, reason, ()) if blocked else (True, "OK", ())

    if not endpoint_allowed:
        if host.lower() in ("localhost", "localhost.localdomain"):
            return False, "localhost is not allowed", ()
        if re.match(r"^[\w.-]+\.(local|lan|internal|private)$", host, re.IGNORECASE):
            return False, f"Host '{host}' appears to be an internal domain", ()

    error, addresses = _resolve_socket_addresses(host)
    if error is not None:
        return False, error, ()
    return _validate_socket_addresses(
        host,
        addresses,
        settings,
        endpoint_allowed=endpoint_allowed,
    )


def validate_endpoint_host(  # noqa: C901
    host: str | None,
    settings: dict | None = None,
    *,
    request_allowed_hosts: Collection[str] | None = None,
) -> tuple[bool, str]:
    """Validate that an endpoint host is not an internal/reserved IP.

    Returns (is_safe, reason).

    Settings are fetched from NetBox plugin with caching:
    - ssrf_protection_enabled: bool (default True)
    - allow_private_ips: bool (default True for on-premises)
    - allowed_ip_ranges: list of CIDR strings
    - blocked_ip_ranges: list of CIDR strings

    IPs already registered in ProxmoxEndpoint or NetBoxEndpoint are automatically allowed.
    """
    if not host:
        return False, "Host cannot be empty"

    host = host.strip()

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and literal.is_unspecified:
        return False, f"Host '{host}' is an unspecified IP address"

    if _is_request_allowed(host, request_allowed_hosts):
        return True, "OK"

    # If domain/host is already registered, allow it
    if is_registered_endpoint(host):
        return True, "OK"

    safe, reason, _addresses = resolve_endpoint_host(host, settings)
    return safe, reason


def validate_endpoint_url(url: str | None, settings: dict | None = None) -> tuple[bool, str]:
    """Validate that a URL doesn't point to an internal resource."""
    if not url:
        return False, "URL cannot be empty"

    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)

        host = parsed.hostname
        if not host:
            return False, "URL has no valid hostname"

        return validate_endpoint_host(host, settings)
    except Exception as e:
        return False, f"Invalid URL: {e}"


def validate_endpoint_host_with_settings(
    host: str | None,
    netbox_session: "Api | None" = None,
) -> tuple[bool, str]:
    """Validate endpoint host using settings fetched from NetBox.

    This is a convenience function that fetches settings and validates.
    """
    from proxbox_api.settings_client import get_settings

    settings = get_settings(netbox_session)
    return validate_endpoint_host(host, settings)
