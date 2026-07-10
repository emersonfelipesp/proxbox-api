"""Cloud-init intent payload normalization."""

import re
from ipaddress import ip_address
from typing import Optional, Union
from urllib.parse import quote

import yaml
from pydantic import BaseModel, field_validator

_DNS_SEARCH_DOMAIN_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?"
)


class CloudInitPayload(BaseModel):
    user: Optional[str] = None
    password: Optional[str] = None
    ssh_keys: Optional[list[str]] = None
    user_data: Optional[Union[str, dict]] = None
    network: Optional[dict] = None
    search_domain: Optional[str] = None
    dns_servers: Optional[list[str]] = None

    @field_validator("search_domain")
    @classmethod
    def validate_search_domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().rstrip(".")
        if not cleaned:
            return None
        if not _DNS_SEARCH_DOMAIN_RE.fullmatch(cleaned):
            raise ValueError("search_domain must be a valid DNS search domain")
        return cleaned

    @field_validator("dns_servers")
    @classmethod
    def validate_dns_servers(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        seen: set[str] = set()
        normalized: list[str] = []
        for item in value:
            server = str(item).strip()
            ip_address(server)
            if server in seen:
                raise ValueError("dns_servers must not contain duplicates")
            seen.add(server)
            normalized.append(server)
        return normalized


def _build_cicustom(user_data: Union[str, dict]) -> str:
    if isinstance(user_data, str):
        stripped = user_data.strip()
        if stripped.startswith(("user=", "network=", "vendor=", "meta=")):
            return user_data
        return f"user={user_data}"

    dumped = yaml.safe_dump(
        user_data,
        default_flow_style=False,
        sort_keys=False,
    )
    return f"user={dumped}"


def _build_ipconfig0(network: dict) -> str | None:
    explicit = network.get("ipconfig0")
    if explicit is not None:
        return str(explicit)

    ip = network.get("ip")
    if ip is None:
        return None

    ip_value = str(ip)
    cidr = network.get("cidr")
    if cidr is not None and "/" not in ip_value and ip_value.lower() not in {"dhcp", "auto"}:
        ip_value = f"{ip_value}/{cidr}"

    parts = [f"ip={ip_value}"]
    gateway = network.get("gw") or network.get("gateway")
    if gateway is not None:
        parts.append(f"gw={gateway}")
    return ",".join(parts)


def build_proxmox_ci_args(payload: CloudInitPayload) -> dict:
    args: dict[str, object] = {}
    if payload.user is not None:
        args["ciuser"] = payload.user
    if payload.password is not None:
        # Proxmox cloud-init password (stored hashed by Proxmox). Enables
        # username+password SSH when the image also permits password auth
        # (ssh_pwauth: true, baked by netbox-packer). Treat as a secret:
        # never log ci_args downstream.
        args["cipassword"] = payload.password
    if payload.ssh_keys is not None:
        args["sshkeys"] = quote("\n".join(payload.ssh_keys), safe="")
    if payload.user_data is not None:
        args["cicustom"] = _build_cicustom(payload.user_data)
    if payload.network is not None:
        ipconfig0 = _build_ipconfig0(payload.network)
        if ipconfig0 is not None:
            args["ipconfig0"] = ipconfig0
    if payload.search_domain is not None:
        args["searchdomain"] = payload.search_domain
    if payload.dns_servers:
        args["nameserver"] = " ".join(payload.dns_servers)
    return args
