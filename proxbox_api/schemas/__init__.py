"""Top-level schema exports and plugin configuration schema."""

from pydantic import field_validator

from proxbox_api.schemas._base import ProxboxBaseModel
from proxbox_api.schemas.firecracker import (
    FirecrackerAssetPrepareRequest as FirecrackerAssetPrepareRequest,
)
from proxbox_api.schemas.firecracker import (
    FirecrackerAssetPrepareResponse as FirecrackerAssetPrepareResponse,
)
from proxbox_api.schemas.firecracker import (
    FirecrackerHostAgentHealth as FirecrackerHostAgentHealth,
)
from proxbox_api.schemas.firecracker import (
    FirecrackerHostCapabilities as FirecrackerHostCapabilities,
)
from proxbox_api.schemas.firecracker import FirecrackerImageBundle as FirecrackerImageBundle
from proxbox_api.schemas.firecracker import (
    FirecrackerMicroVMAction as FirecrackerMicroVMAction,
)
from proxbox_api.schemas.firecracker import (
    FirecrackerMicroVMCreateRequest as FirecrackerMicroVMCreateRequest,
)
from proxbox_api.schemas.firecracker import (
    FirecrackerMicroVMMetrics as FirecrackerMicroVMMetrics,
)
from proxbox_api.schemas.firecracker import FirecrackerMicroVMState as FirecrackerMicroVMState
from proxbox_api.schemas.firecracker import FirecrackerNetworkMode as FirecrackerNetworkMode
from proxbox_api.schemas.firecracker import (
    FirecrackerNetworkRequest as FirecrackerNetworkRequest,
)
from proxbox_api.schemas.firecracker import (
    FirecrackerProvisionRequest as FirecrackerProvisionRequest,
)
from proxbox_api.schemas.firecracker import (
    FirecrackerProvisionResponse as FirecrackerProvisionResponse,
)

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
