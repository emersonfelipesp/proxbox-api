"""Schemas for writing Proxmox InfluxDB metric-server settings."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

CONFIG_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 ._\-]{0,127}$"
_PATH_PREFIX_PATTERN = r"^/?[A-Za-z0-9._\-]+(/[A-Za-z0-9._\-]+)*/?$"
_HOST_PATTERN = r"^[A-Za-z0-9]([A-Za-z0-9.:\-\[\]]{0,253}[A-Za-z0-9\]])?$"


class InfluxMetricServerUpdate(BaseModel):
    """Allow-listed InfluxDB metric-server parameters accepted by Proxmox.

    Proxmox requires ``server`` and ``port`` on every update. ``token`` is
    required too: Proxmox keeps the stored credential when a request omits it,
    so any change to the destination, bucket, organization or certificate
    verification could otherwise send that credential somewhere new. Unknown
    fields are rejected so a caller cannot push arbitrary parameters to the
    Proxmox cluster through this route.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    server: str = Field(pattern=_HOST_PATTERN)
    port: int = Field(ge=1, le=65535)
    influxdbproto: Literal["udp", "http", "https"] | None = None
    organization: str | None = Field(default=None, pattern=_NAME_PATTERN)
    bucket: str | None = Field(default=None, pattern=_NAME_PATTERN)
    token: SecretStr = Field(min_length=1, max_length=512)
    api_path_prefix: str | None = Field(
        default=None, alias="api-path-prefix", max_length=128, pattern=_PATH_PREFIX_PATTERN
    )
    max_body_size: int | None = Field(default=None, alias="max-body-size", ge=1, le=100_000_000)
    mtu: int | None = Field(default=None, ge=512, le=65_536)
    timeout: int | None = Field(default=None, ge=1, le=600)
    verify_certificate: bool | None = Field(default=None, alias="verify-certificate")
    disable: bool | None = None

    def pve_payload(self) -> dict[str, object]:
        """Return the Proxmox request body, unwrapping the secret token."""
        payload: dict[str, object] = {}
        for name, field in type(self).model_fields.items():
            value = getattr(self, name)
            if value is None:
                continue
            key = field.alias or name
            if isinstance(value, SecretStr):
                payload[key] = value.get_secret_value()
            elif isinstance(value, bool):
                payload[key] = int(value)
            else:
                payload[key] = value
        return payload

    def field_names(self) -> list[str]:
        """Return the Proxmox parameter names being set, never their values."""
        return sorted(self.pve_payload())


class InfluxMetricServerWriteResponse(BaseModel):
    """Secret-safe result of one metric-server write."""

    status: Literal["pushed"]
    endpoint_id: int | None = None
    cluster_name: str | None = None
    actor: str
    path: str
    fields: list[str]
