"""Top-level schema exports and plugin configuration schema."""

from pydantic import field_validator

from proxbox_api.schemas._base import ProxboxBaseModel

from .image_factory import ImageFactoryBuildMode as ImageFactoryBuildMode
from .image_factory import PackerImageBuildRequest as PackerImageBuildRequest
from .image_factory import PackerImageBuildResponse as PackerImageBuildResponse
from .netbox import NetboxSessionSchema
from .proxmox import ProxmoxSessionSchema


class PluginConfig(ProxboxBaseModel):
    proxmox: list[ProxmoxSessionSchema]
    netbox: NetboxSessionSchema

    @field_validator("proxmox", mode="before")
    @classmethod
    def normalize_proxmox(cls, value: object) -> list[ProxmoxSessionSchema]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]  # type: ignore[list-item]
