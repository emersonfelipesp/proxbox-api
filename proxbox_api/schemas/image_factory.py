"""Image factory request and response schemas."""

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ImageFactoryBuildMode(str, Enum):
    direct_cloud_image = "direct-cloud-image"
    packer_clone = "packer-clone"
    packer_iso = "packer-iso"


class PackerImageBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: int
    target_node: str
    builder_type: Literal["proxmox-clone", "proxmox-iso"] = "proxmox-clone"
    # clone-specific (required for proxmox-clone)
    template_vmid: int | None = None
    output_vmid: int
    output_name: str
    os_family: str
    os_release: str
    image_version: str
    vm_storage: str
    cloud_init_storage: str | None = None
    bridge: str = "vmbr0"
    memory_mb: int = 2048
    cores: int = 2
    cpu_type: str = "host"
    provisioner_recipe: str
    variables: dict[str, str | int | bool] = Field(default_factory=dict)
    force: bool = False
    dry_run: bool = False
    # iso-specific (required when builder_type == "proxmox-iso")
    iso_file: str | None = None
    iso_checksum: str | None = None
    iso_storage: str | None = None


class PackerImageBuildResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build_id: str
    status: Literal["queued", "running", "failed", "completed", "cancelled"]
    endpoint_id: int
    target_node: str
    output_vmid: int
    output_name: str
    artifact_template_name: str | None = None
    packer_template_path: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    log_url: str | None = None
