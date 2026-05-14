"""Cloud-init intent payload normalization."""

from typing import Optional, Union
from urllib.parse import quote

import yaml
from pydantic import BaseModel


class CloudInitPayload(BaseModel):
    user: Optional[str] = None
    ssh_keys: Optional[list[str]] = None
    user_data: Optional[Union[str, dict]] = None
    network: Optional[dict] = None


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
    if payload.ssh_keys is not None:
        args["sshkeys"] = quote("\n".join(payload.ssh_keys), safe="")
    if payload.user_data is not None:
        args["cicustom"] = _build_cicustom(payload.user_data)
    if payload.network is not None:
        ipconfig0 = _build_ipconfig0(payload.network)
        if ipconfig0 is not None:
            args["ipconfig0"] = ipconfig0
    return args
